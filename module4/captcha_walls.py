"""hCaptcha walls on an ATS's email / sign-in gate: park the job, charge nothing.

Measured 2026-10-18 (GHO-54). Oracle Cloud Recruiting (*.oraclecloud.com,
Candidate Experience) and iCIMS (*.icims.com) put an INVISIBLE hCaptcha on the
"enter your email" step, the gate in front of the whole application. In a
fresh scratch Chromium (new profile, headed, not the jobo profile, never used
for automation, email typed at human speed, a real mouse click on Next) both
showed the full image challenge (a 520x570 frame) on the first Next. So it is
not our profile, session or speed: every visitor without an hCaptcha
reputation gets the puzzle. The agent has never solved one (Oracle: 4 jobs, 4
walls, 0 submissions ever), and each attempt spent 15-17 model steps finding
that out.

Lever and other forms load the same invisible widget but challenge only some
visitors (45 Lever applications went through), so this park is limited to the
gate families below; a challenge elsewhere stays an ordinary captcha_failed.

How a job gets here:
  * in a run, hermes_plugin/jobo-browser/captcha_guard.py sees the challenge on
    one of these hosts and tells the agent to end with STATUS: captcha_wall;
  * before a run, a job whose job_url host already parked another job is
    parked without an agent (walled_job_hosts).

A parked job keeps should_apply and its attempts; runs skip it. It needs a
policy call from G to go further (no captcha-solving service without
asking G). `--job-id` / `--force` still reach it, for testing.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Keep in sync with GATE_HOST_SUFFIXES in hermes_plugin/jobo-browser/captcha_guard.py
# (the plugin cannot import module4); scripts/test_hcaptcha_walls.py checks it.
GATE_HOST_SUFFIXES: tuple[str, ...] = ("oraclecloud.com", "icims.com")

STATUS_PREFIX = "captcha_wall:"

_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)


def host_of(url: str | None) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_gate_host(host: str | None) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in GATE_HOST_SUFFIXES)


def gate_host_in_text(text: str | None) -> str | None:
    """The first gate-family host named in free text (an agent DETAIL), or None."""
    return next((h.lower() for h in _HOST.findall(text or "") if is_gate_host(h)), None)


def status_for(host: str, detail: str) -> str:
    """module4_last_status for a parked job: 'captcha_wall:<host> | <detail>'."""
    return f"{STATUS_PREFIX}{host} | {detail}"[:600]


def is_parked(last_status: str | None) -> bool:
    s = (last_status or "").strip()
    return s.startswith(STATUS_PREFIX) and is_gate_host(s[len(STATUS_PREFIX):].split("|", 1)[0].strip())


def own_host(url: str | None) -> str:
    """job_url host, or '' for LinkedIn (its jobs go to many different ATSes)."""
    h = host_of(url)
    return "" if not h or h == "linkedin.com" or h.endswith(".linkedin.com") else h


def walled_job_hosts(rows) -> set[str]:
    """job_url hosts of parked jobs. Another job on one of them meets the same wall."""
    return {
        h for r in rows
        if is_parked(getattr(r, "module4_last_status", None))
        and (h := own_host(getattr(r, "job_url", None)))
    }


def walled_host_for_url(url: str | None, walled: set[str]) -> str | None:
    h = own_host(url)
    return h if h and h in walled else None
