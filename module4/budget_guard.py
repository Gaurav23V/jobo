"""Weekly Claude budget guard (GHO-2).

The owner's Claude account is a weekly subscription shared across every
jobo stage (module3's `claude -p` calls and module4's `hermes chat` agent).
Anthropic's own authenticated `GET /api/oauth/usage` response carries the
same number the `anthropic-ratelimit-unified-7d-utilization` header family
exposes on live Messages API responses - this endpoint is the free,
dedicated way to read it (no model tokens spent), so it is checked here
instead of sniffing headers off a real completion call, which would cost
real budget on every single between-jobs check.

Verified live 2026-10-14: `seven_day.utilization` in the JSON body was 47.0
(a percent, 0-100) while the account showed 47% weekly usage in the
Claude Code UI at the same moment.

The same payload carries `five_hour` (verified live 2026-10-15: {"utilization":
9.0, "resets_at": "2026-10-15T16:29:59.632385+00:00"}), the subscription's
rolling 5-hour session window. It is the one that actually stopped the
15 Oct 14:29 round (GHO-22): 4 workers burned the whole window in ~40 min
while the weekly figure read 1%, and every agent in flight died mid-form on
"You've hit your session limit". The apply loop reads it between jobs with
its own threshold and pauses claims until the window resets
(`session_pause_seconds`) instead of letting agents start into it.

Credentials are read directly from the standard Claude Code credential
file (~/.claude/.credentials.json), not through hermes's internal Python
package - hermes's own dependency environment was found broken on this box
during development (`hermes --help` -> "no dependency environment is
committed for this install"), so a guard that stops real applications must
not depend on hermes's internal modules being importable.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DEFAULT_THRESHOLD = 0.50  # stop at 50% weekly usage (was 0.75)
# 5-hour session window. On 15 Oct 4 workers burned ~2.5% of it a minute and
# a job runs ~7 min, so the jobs already in flight when the gate trips need
# roughly the last 20% to finish.
# Stop at 30% of the 5-hour session (the same account is used for other
# work). At that line the apply stage stops; it does not pause and
# resume after the reset unless JOBO_SESSION_PAUSE=1 is set for the run.
DEFAULT_SESSION_THRESHOLD = 0.30
SESSION_PAUSE_ENV = "JOBO_SESSION_PAUSE"
# Wait this long past the reported reset before re-reading usage, and only
# pause if at least SESSION_MIN_WORK_SEC of the stage is left after that.
SESSION_RESET_SLACK_SEC = 60.0
SESSION_MIN_WORK_SEC = 600.0
CREDENTIALS_RELPATH = ".claude/.credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _passwd_home() -> Path:
    # Not Path.home()/$HOME: this process may run with HOME already
    # redirected (e.g. inside an agent sandbox or the regression harness).
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def credentials_path(host_home: Path | None = None) -> Path:
    return (host_home or _passwd_home()) / CREDENTIALS_RELPATH


def read_access_token(host_home: Path | None = None) -> str | None:
    path = credentials_path(host_home)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")
    if not token:
        return None
    if expires_at and time.time() * 1000 >= expires_at:
        return None  # expired; a refresh is Claude Code's job, not ours
    return token


def _ms_to_iso(ms) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def token_problem(host_home: Path | None = None) -> str:
    """Why read_access_token() found no usable token (GHO-77): no file, no
    token, or expired (and when). Never includes the token itself."""
    path = credentials_path(host_home)
    try:
        data = json.loads(path.read_text())
    except OSError:
        return f"no credentials file at {path}"
    except json.JSONDecodeError:
        return f"credentials file {path} is not JSON"
    oauth = (data.get("claudeAiOauth") if isinstance(data, dict) else None) or {}
    if not oauth.get("accessToken"):
        return f"no OAuth access token in {path}"
    msg = f"OAuth access token in {path} expired at {_ms_to_iso(oauth.get('expiresAt'))}"
    refresh_expires = oauth.get("refreshTokenExpiresAt")
    if not oauth.get("refreshToken"):
        msg += ", and there is no refresh token"
    elif refresh_expires and time.time() * 1000 >= refresh_expires:
        msg += f", refresh token expired at {_ms_to_iso(refresh_expires)}"
    return msg


def fetch_usage(token: str, *, timeout: float = 15.0) -> tuple[dict | None, str | None]:
    """(usage payload, error) from the usage endpoint.

    Unreadable (network error, bad token, unexpected payload shape) is
    deliberately no payload with a short error, never a guessed 0.0 or 1.0 -
    the caller must treat "I don't know" as "don't guess", the same way the
    existing Hermes usage guard logs-and-retries rather than ever treating an
    unreadable read as a breach (or as all-clear).
    """
    payload, error, _ = fetch_usage_detail(token, timeout=timeout)
    return payload, error


def retry_after_seconds(value: str | None, *, now: float | None = None) -> float | None:
    """A Retry-After header (delta seconds or an HTTP date) as seconds, or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def fetch_usage_detail(
    token: str, *, timeout: float = 15.0
) -> tuple[dict | None, str | None, float | None]:
    """fetch_usage plus the server's Retry-After (seconds) when it sent one."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        retry = retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
        return None, f"usage endpoint unreadable: {type(e).__name__}: {e}"[:200], retry
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return None, f"usage endpoint unreadable: {type(e).__name__}: {e}"[:200], None
    if not isinstance(payload, dict):
        return None, "usage payload is not a JSON object", None
    return payload, None, None


def fetch_seven_day_usage(
    token: str, *, timeout: float = 15.0
) -> tuple[float | None, str | None, str | None]:
    """(utilization 0.0-1.0, window resets_at, error) for the weekly window."""
    payload, error = fetch_usage(token, timeout=timeout)
    if payload is None:
        return None, None, error
    return parse_usage_payload(payload)


def _parse_window(
    payload: object, key: str
) -> tuple[float | None, str | None, str | None]:
    window = (payload.get(key) if isinstance(payload, dict) else None) or {}
    resets_at = window.get("resets_at") or None
    try:
        return float(window["utilization"]) / 100.0, resets_at, None
    except (KeyError, TypeError, ValueError):
        return None, resets_at, f"usage payload has no numeric {key}.utilization"


def parse_usage_payload(
    payload: object,
) -> tuple[float | None, str | None, str | None]:
    """The weekly window: (utilization 0.0-1.0, resets_at, error)."""
    return _parse_window(payload, "seven_day")


def parse_session_payload(
    payload: object,
) -> tuple[float | None, str | None, str | None]:
    """The 5-hour session window: (utilization 0.0-1.0, resets_at, error)."""
    return _parse_window(payload, "five_hour")


def fetch_seven_day_utilization(
    token: str, *, timeout: float = 15.0
) -> float | None:
    """The fraction (0.0-1.0) of the weekly window used, or None if unreadable."""
    return fetch_seven_day_usage(token, timeout=timeout)[0]


@dataclass
class BudgetCheck:
    utilization: float | None  # None = unreadable this check
    threshold: float
    should_stop: bool  # utilization >= threshold, unless overridden
    resets_at: str | None = None  # weekly window reset (ISO 8601), if reported
    overridden: bool = False  # the user explicitly asked for this run (JOBO_BUDGET_OVERRIDE)
    error: str | None = None  # why utilization is None
    # The 5-hour session window (GHO-22). Not part of should_stop: the apply
    # loop pauses on it until the window resets rather than ending the run,
    # and JOBO_BUDGET_OVERRIDE does not lift it - it is the provider's hard
    # limit, not the user's weekly rule.
    session_utilization: float | None = None  # None = unreadable
    session_threshold: float = DEFAULT_SESSION_THRESHOLD
    session_resets_at: str | None = None
    # The in-run read (check_budget_in_run, GHO-86) only. `blind`: this
    # call's read failed; the windows above are the last good reading, if
    # recent enough, with `reading_age` its age in seconds. `blind_stop`:
    # reads have failed too long, or the last good session reading was
    # near the line - claim no new job. `notice`: a line to log once per
    # blind stretch (its start and its end), else None.
    blind: bool = False
    reading_age: float | None = None
    blind_stop: str | None = None
    notice: str | None = None

    @property
    def session_limited(self) -> bool:
        """The 5-hour window is at or past its threshold: start no new agent."""
        return (
            self.session_utilization is not None
            and self.session_utilization >= self.session_threshold
        )


def decide(
    utilization: float | None,
    threshold: float,
    *,
    resets_at: str | None = None,
    override: bool = False,
    error: str | None = None,
    session_utilization: float | None = None,
    session_threshold: float = DEFAULT_SESSION_THRESHOLD,
    session_resets_at: str | None = None,
) -> BudgetCheck:
    should_stop = (
        not override and utilization is not None and utilization >= threshold
    )
    return BudgetCheck(
        utilization=utilization,
        threshold=threshold,
        should_stop=should_stop,
        resets_at=resets_at,
        overridden=override,
        error=error,
        session_utilization=session_utilization,
        session_threshold=session_threshold,
        session_resets_at=session_resets_at,
    )


def check_budget(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    host_home: Path | None = None,
    override: bool = False,
    session_threshold: float | None = None,
) -> BudgetCheck:
    if session_threshold is None:
        session_threshold = session_threshold_from_env()
    token = read_access_token(host_home)
    if not token:
        return decide(
            None,
            threshold,
            override=override,
            error=f"no unexpired OAuth token in {credentials_path(host_home)}",
            session_threshold=session_threshold,
        )
    payload, error = fetch_usage(token)
    if payload is None:
        return decide(
            None, threshold, override=override, error=error,
            session_threshold=session_threshold,
        )
    utilization, resets_at, error = parse_usage_payload(payload)
    session, session_resets_at, _ = parse_session_payload(payload)
    return decide(
        utilization, threshold, resets_at=resets_at, override=override, error=error,
        session_utilization=session, session_threshold=session_threshold,
        session_resets_at=session_resets_at,
    )


# -- the in-run read (GHO-86) --------------------------------------------------
# On 23 Oct 22:00 four lanes read the endpoint before every job and 45 of 79
# reads got HTTP 429; an unreadable read meant "proceed", so the 5h gate ran
# blind for two 6-minute stretches while session usage went 11% -> 25%.
# Lanes are separate processes, so they share one reading through a file.
USAGE_CACHE_ENV = "JOBO_USAGE_CACHE"
USAGE_CACHE_TTL_SEC = 60.0  # a reading this fresh is reused without a request
BACKOFF_BASE_SEC = 60.0  # after a failed read with no Retry-After, doubling per failure
BACKOFF_MAX_SEC = 300.0
STALE_LIMIT_SEC = 600.0  # no good reading for longer than this: claim no new job
SESSION_MARGIN = 0.05  # a last good session reading this close to the line: claim no new job
# No failed read for this long: the old stretch belongs to an earlier run.
# Within a run lanes read at least once per job, well inside an hour.
STRETCH_GAP_SEC = 3600.0


def usage_cache_path() -> Path:
    raw = os.environ.get(USAGE_CACHE_ENV, "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "data" / "claude_usage_cache.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """One reader at a time across lanes; the others then reuse its reading."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _backoff(retry_after: float | None, failures: int) -> float:
    if retry_after is not None:
        return min(retry_after, BACKOFF_MAX_SEC)
    return min(BACKOFF_BASE_SEC * 2 ** max(0, failures - 1), BACKOFF_MAX_SEC)


def _fetch_with_token(host_home: Path | None) -> tuple[dict | None, str | None, float | None]:
    token = read_access_token(host_home)
    if not token:
        return None, f"no unexpired OAuth token in {credentials_path(host_home)}", None
    return fetch_usage_detail(token)


def _minutes(sec: float) -> str:
    return f"{sec / 60:.0f} min"


def check_budget_in_run(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    override: bool = False,
    session_threshold: float | None = None,
    host_home: Path | None = None,
    cache_path: Path | None = None,
    fetcher: Callable[[], tuple[dict | None, str | None, float | None]] | None = None,
    now: Callable[[], float] = time.time,
) -> BudgetCheck:
    """The apply stage's between-jobs usage read, shared by every lane.

    A reading under USAGE_CACHE_TTL_SEC old is reused without a request.
    After a failed read no lane asks again before Retry-After (or a
    doubling backoff) has passed. While reads fail, decisions use the last
    good reading if it is under STALE_LIMIT_SEC old, and `blind_stop` is
    set once there has been no good reading for longer than that, or when
    the last good session reading in the same 5h window was within
    SESSION_MARGIN of the line. The pipeline-level `jobo budget-gate` keeps check_budget, where
    unreadable still means proceed.

    `fetcher() -> (payload, error, retry_after_sec)` and `now` are the test
    seams; the default fetcher reads the token and calls the endpoint.
    """
    if session_threshold is None:
        session_threshold = session_threshold_from_env()
    path = cache_path or usage_cache_path()
    fetcher = fetcher or (lambda: _fetch_with_token(host_home))
    notice = None
    with _locked(path):
        state = _load_state(path)
        t = now()
        good = state.get("good")
        age = t - good["ts"] if good else None
        fresh = age is not None and 0 <= age < USAGE_CACHE_TTL_SEC
        if not fresh and t >= state.get("retry_at", 0.0):
            payload, error, retry_after = fetcher()
            if payload is not None:
                if state.get("blind_since") is not None:
                    notice = (
                        f"Claude usage readable again after {_minutes(t - state['blind_since'])} "
                        f"of failed reads ({state.get('failures', 0)} failed)."
                    )
                state = {"good": {"ts": t, "payload": payload}}
                good, age, fresh = state["good"], 0.0, True
            else:
                if t - state.get("last_fail", t) > STRETCH_GAP_SEC:
                    # A stretch left over from an earlier run: start a new one.
                    state = {"good": good} if good else {}
                state["failures"] = state.get("failures", 0) + 1
                state["error"] = error
                state["last_fail"] = t
                state["retry_at"] = t + _backoff(retry_after, state["failures"])
                if state.get("blind_since") is None:
                    state["blind_since"] = t
        if fresh:
            _save_state(path, state)
            return _decide_payload(
                good["payload"], threshold, override, session_threshold,
                reading_age=age, notice=notice,
            )

        # Blind since the last good reading, when that came in the same run;
        # since the first failed read when it is from an earlier one.
        anchor = state["blind_since"]
        if good is not None and anchor - good["ts"] <= STRETCH_GAP_SEC:
            anchor = good["ts"]
        blind_for = t - anchor
        error = state.get("error") or "unknown error"
        recent = good is not None and age <= STALE_LIMIT_SEC
        if recent:
            check = _decide_payload(
                good["payload"], threshold, override, session_threshold, reading_age=age,
            )
            check.error = check.error or error
        else:
            check = decide(None, threshold, override=override, error=error,
                           session_threshold=session_threshold)
        check.blind = True
        if not state.get("noticed"):
            state["noticed"] = True
            last = f"the last good reading ({_minutes(age)} old)" if recent else "no recent reading"
            check.notice = (
                f"Claude usage reads failing ({error}); using {last}. Logged once until "
                f"reads recover; no new job is claimed after {_minutes(STALE_LIMIT_SEC)} "
                f"without a good reading."
            )
        if blind_for > STALE_LIMIT_SEC:
            check.blind_stop = (
                f"no good Claude usage reading for {_minutes(blind_for)} "
                f"(limit {_minutes(STALE_LIMIT_SEC)}; {error})"
            )
        else:
            last_session, last_resets, _ = parse_session_payload(good["payload"]) if good else (None, None, None)
            same_window = (_reset_epoch(last_resets) or 0.0) > t
            if same_window and last_session is not None and last_session >= session_threshold - SESSION_MARGIN:
                check.blind_stop = (
                    f"Claude usage unreadable ({error}) and the last good 5h session reading, "
                    f"{last_session:.0%}, was within {SESSION_MARGIN * 100:.0f} points of the "
                    f"{session_threshold:.0%} line"
                )
        _save_state(path, state)
        return check


def _decide_payload(payload, threshold, override, session_threshold, *, reading_age, notice=None) -> BudgetCheck:
    utilization, resets_at, error = parse_usage_payload(payload)
    session, session_resets_at, _ = parse_session_payload(payload)
    check = decide(
        utilization, threshold, resets_at=resets_at, override=override, error=error,
        session_utilization=session, session_threshold=session_threshold,
        session_resets_at=session_resets_at,
    )
    check.reading_age = reading_age
    check.notice = notice
    return check


def threshold_from_env(
    default: float = DEFAULT_THRESHOLD, *, env: str = "JOBO_BUDGET_THRESHOLD"
) -> float:
    raw = os.environ.get(env)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def session_threshold_from_env(default: float = DEFAULT_SESSION_THRESHOLD) -> float:
    return threshold_from_env(default, env="JOBO_SESSION_THRESHOLD")


def session_pause_enabled() -> bool:
    """True only if this run may wait for the 5h window to reset and go on.

    Off by default (the user's rule: stop at the session line, don't resume on the
    next window by itself).
    """
    return os.environ.get(SESSION_PAUSE_ENV, "").strip().lower() in {"1", "true", "yes"}


def _reset_epoch(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def session_pause_seconds(
    resets_at: str | None,
    *,
    now: float,
    seconds_to_deadline: float,
    slack: float = SESSION_RESET_SLACK_SEC,
    min_work: float = SESSION_MIN_WORK_SEC,
) -> float | None:
    """How long to pause claims for the 5-hour window to reset, or None to stop.

    `now` is wall-clock epoch seconds. None when the reset time is unknown or
    lands too close to the stage deadline to leave `min_work` seconds of
    applying afterwards: stopping cleanly then is the better outcome.
    """
    reset = _reset_epoch(resets_at)
    if reset is None:
        return None
    wait = max(0.0, reset - now) + slack
    if wait + min_work > seconds_to_deadline:
        return None
    return wait


def usage_summary(check: BudgetCheck) -> str:
    """'5h session 45% (resets ...), weekly 3%' - the burn-rate log line."""
    if check.session_utilization is None:
        session = "5h session UNREADABLE"
    else:
        session = f"5h session {check.session_utilization * 100:.0f}%"
        if check.session_resets_at:
            session += f" (resets {check.session_resets_at})"
    if check.utilization is None:
        weekly = f"weekly UNREADABLE ({check.error or 'unknown error'})"
    else:
        weekly = f"weekly {check.utilization * 100:.0f}%"
    if check.blind and check.reading_age is not None:
        weekly += f" [last good reading, {_minutes(check.reading_age)} old; reads failing]"
    return f"{session}, {weekly}"


OVERRIDE_ENV = "JOBO_BUDGET_OVERRIDE"


def override_from_env() -> bool:
    """True only when the user explicitly asked for a run despite the weekly budget.

    Set per invocation (`JOBO_BUDGET_OVERRIDE=1 bash scripts/daily_pipeline.sh`
    or `bash scripts/daily_pipeline.sh --budget-override`), never in cron's
    environment: it disables every budget stop for that one process tree,
    including module4's between-jobs check.
    """
    return os.environ.get(OVERRIDE_ENV, "").strip().lower() in {"1", "true", "yes"}


# `jobo budget-gate` exit status that means "do not run this stage".
# EX_TEMPFAIL: the condition clears on its own when the weekly window resets.
GATE_STOP_EXIT = 75


def gate_record(check: BudgetCheck, stage: str) -> dict:
    """The structured gate result daily_pipeline.sh embeds in its verdict line."""
    if check.should_stop:
        decision = "SKIP"
    elif check.session_limited:
        decision = "SKIP_SESSION"
    elif check.utilization is None:
        decision = "PROCEED_UNREADABLE"
    elif check.overridden and check.utilization >= check.threshold:
        decision = "OVERRIDE"
    else:
        decision = "PROCEED"
    return {
        "gate": stage,
        "decision": decision,
        "utilization": check.utilization,
        "threshold": check.threshold,
        "resets_at": check.resets_at,
        "override": check.overridden,
        "error": check.error,
        "session_utilization": check.session_utilization,
        "session_resets_at": check.session_resets_at,
    }


def _pct(value: float | None) -> str:
    return "unreadable" if value is None else f"{value * 100:.0f}%"


def gate_message(check: BudgetCheck, stage: str) -> str:
    """One human log line for the run log - says what was read and what happens."""
    msg = _weekly_gate_message(check, stage)
    if check.session_limited and not check.should_stop:
        resets = f" (resets {check.session_resets_at})" if check.session_resets_at else ""
        return (
            f"BUDGET GATE ({stage}): 5h session usage "
            f"{check.session_utilization * 100:.0f}% >= "
            f"{check.session_threshold * 100:.0f}% session line{resets} - SKIPPING "
            f"(the user shares this account; weekly {_pct(check.utilization)})."
        )
    if check.session_utilization is not None:
        resets = f" (resets {check.session_resets_at})" if check.session_resets_at else ""
        msg += (
            f" 5h session usage {check.session_utilization * 100:.0f}%{resets}; apply "
            f"pauses claims at {check.session_threshold * 100:.0f}%."
        )
    return msg


def _weekly_gate_message(check: BudgetCheck, stage: str) -> str:
    head = f"BUDGET GATE ({stage}):"
    resets = f", weekly window resets {check.resets_at}" if check.resets_at else ""
    limit = f"{check.threshold * 100:.0f}% threshold"
    if check.utilization is None:
        usage = f"weekly Claude usage UNREADABLE ({check.error or 'unknown error'})"
        tail = "proceeding as before; apply still re-checks between jobs."
        if check.overridden:
            tail = f"{OVERRIDE_ENV} set; proceeding."
        return f"{head} {usage} - {tail}"
    usage = f"weekly Claude usage {check.utilization * 100:.0f}%"
    if check.should_stop:
        return (
            f"{head} {usage} >= {limit}{resets} - SKIPPING. Runs resume on their "
            f"own once usage drops below the threshold; force one run only when the user "
            f"explicitly asks, with {OVERRIDE_ENV}=1 or --budget-override."
        )
    if check.overridden and check.utilization >= check.threshold:
        return (
            f"{head} {usage} >= {limit}{resets} but {OVERRIDE_ENV} is set (the user asked "
            f"for this run) - proceeding; apply's between-jobs stop is off too."
        )
    return f"{head} {usage} < {limit}{resets} - proceeding."


def write_resume_marker(
    path: Path, *, check: BudgetCheck, stage: str, jobs_done: int, jobs_remaining: int
) -> None:
    """A human/next-run-readable record of why the stage stopped early.

    The actual resumable state is the DB itself (should_apply=1 rows not yet
    applied are untouched and idempotently retried next run) - this marker
    exists so "why did the run stop" is answered without reading the log.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "stage": stage,
                "reason": "weekly_budget_threshold",
                "utilization": check.utilization,
                "threshold": check.threshold,
                "jobs_done_this_stage": jobs_done,
                "jobs_remaining_this_stage": jobs_remaining,
                "resume": "re-run `uv run jobo apply`; should_apply=1 rows not yet "
                "applied are untouched and will be retried idempotently.",
            },
            indent=2,
        )
        + "\n"
    )


RESUME_MARKER_PATH = Path("data") / "pipeline_budget_pause.json"
