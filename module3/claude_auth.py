"""Can Claude authenticate at all? (GHO-77)

On 22 Oct the host's Claude login had expired hours before the 10:00 run. The
budget gate read that as "usage unreadable" and proceeded, triage spent 462
`claude -p` calls (3 per job) on "Failed to authenticate: OAuth session expired
and could not be refreshed", and still reported OK.

The access token in ~/.claude/.credentials.json expires every few hours and
`claude` refreshes it on its next call, so an expired token in the file proves
nothing by itself. A token that is still valid is taken as logged in (free);
anything else gets one tiny `claude -p` call, which refreshes the login if it
still can. Only an authentication error from that call fails the gate; a
timeout or any other error is "unknown" and the run proceeds as before.

Refreshing or repairing the login is the host's job (`claude /login`), never
jobo's.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from module4 import budget_guard

# What `claude` prints when it cannot authenticate. Kept specific to the
# Claude CLI and the Anthropic API, because run-report also scans whole run
# logs with it and an apply agent's text can mention an employer's sign-in.
AUTH_ERROR_RE = re.compile(
    r"failed to authenticate(?:[.:]|$)"
    r"|oauth (?:session|token) (?:has )?(?:expired|been revoked|was revoked)"
    r"|could not be refreshed"
    r"|please run /login"
    r"|invalid api key"
    r"|authentication_error",
    re.IGNORECASE,
)

# `jobo budget-gate` and `jobo triage` exit status for "Claude can't
# authenticate" (EX_NOPERM). daily_pipeline.sh stops the run on it.
AUTH_EXIT = 77
PROBE_TIMEOUT_SEC = 90
PROBE_MODEL = "haiku"
FIX_HINT = "log in again on the host (`claude /login`); jobo does not refresh the login"


class ClaudeAuthError(RuntimeError):
    """`claude` answered, and the answer was an authentication failure."""


def is_auth_error(text: str | None) -> bool:
    return bool(text and AUTH_ERROR_RE.search(text))


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


@dataclass
class AuthCheck:
    # OK: a valid token in the credentials file, no call made.
    # PROBED_OK: the probe call went through (and refreshed the login).
    # FAILED: the probe call came back with an authentication error.
    # UNKNOWN: the probe timed out or failed some other way; proceed.
    decision: str
    detail: str
    probed: bool = False

    @property
    def failed(self) -> bool:
        return self.decision == "FAILED"

    def record(self) -> dict:
        return asdict(self)


def probe_claude(*, timeout: float = PROBE_TIMEOUT_SEC) -> tuple[bool | None, str]:
    """One minimal `claude -p` call: (True, _) worked, (False, why) auth failed,
    (None, why) something else went wrong."""
    from module3 import claude_client

    try:
        cmd = [
            claude_client._claude_bin(),
            "-p",
            "--model", PROBE_MODEL,
            "--system-prompt", "Reply with the single word OK.",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format", "json",
        ]
    except RuntimeError as e:
        return None, str(e)
    with tempfile.TemporaryDirectory(prefix="jobo-claude-auth-") as tmp:
        try:
            proc = subprocess.run(
                cmd, input="OK?", capture_output=True, text=True, timeout=timeout, cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return None, f"auth probe timed out after {timeout:.0f}s"
        except OSError as e:
            return None, f"auth probe could not start claude: {e}"
    return classify_probe(proc.returncode, proc.stdout or "", proc.stderr or "")


def classify_probe(returncode: int, stdout: str, stderr: str) -> tuple[bool | None, str]:
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        text = str(envelope.get("result") or "")
        if not envelope.get("is_error") and returncode == 0:
            return True, "auth probe answered"
    else:
        text = f"{stdout}\n{stderr}"
    if is_auth_error(text) or is_auth_error(stderr):
        return False, _one_line(text if is_auth_error(text) else stderr)
    return None, f"auth probe failed (exit {returncode}): {_one_line(text or stderr, 200)}"


def check_auth(host_home: Path | None = None, *, probe=None) -> AuthCheck:
    if budget_guard.read_access_token(host_home):
        return AuthCheck("OK", "unexpired OAuth token in the credentials file")
    why = budget_guard.token_problem(host_home)
    ok, detail = (probe or probe_claude)()
    if ok:
        return AuthCheck("PROBED_OK", f"{why}; a probe call went through", probed=True)
    if ok is False:
        return AuthCheck("FAILED", f"{why}; probe call: {detail}", probed=True)
    return AuthCheck("UNKNOWN", f"{why}; {detail}", probed=True)


def gate_message(check: AuthCheck, stage: str) -> str:
    head = f"CLAUDE AUTH GATE ({stage}):"
    if check.decision == "FAILED":
        return (
            f"{head} FAILED - Claude can't authenticate ({check.detail}). Stopping the run "
            f"before any Claude stage (claude_auth); {FIX_HINT}."
        )
    if check.decision == "UNKNOWN":
        return f"{head} UNKNOWN ({check.detail}) - proceeding as before."
    if check.decision == "PROBED_OK":
        return f"{head} OK ({check.detail})."
    return ""  # a valid token: nothing worth a log line
