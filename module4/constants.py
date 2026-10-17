"""Module 4 - autonomous application submission via a Hermes-driven browser.

Design: thin orchestration around an agent. The LLM handles arbitrary page
flows (Easy Apply modals, external ATS portals, novel screener questions);
this code owns the queue, pacing, logging, and DB state.
"""

from __future__ import annotations

import os
from pathlib import Path


def browser_profile_dir() -> Path:
    """LinkedIn-signed-in Chromium profile used by the apply browser.

    Defaults to ~/jobo-linkedin-profile (/home/<user>/... on Linux,
    /Users/<user>/... on a Mac).
    """
    raw = os.environ.get("JOBO_BROWSER_PROFILE", "").strip()
    return Path(raw) if raw else Path.home() / "jobo-linkedin-profile"


def apply_delay_seconds() -> int:
    """Base delay after a REAL submission (human-like pacing).

    Was 90s until 2026-09-02. Measured on the 2026-09-01 run: 62 sleeps at
    ~112s average burned 1.94h of a 4h apply window - nearly half the budget -
    and most of those sleeps followed dead listings or circuit-broken skips
    where nothing was submitted and there was no ATS rate-limit surface to
    space out at all. The delay now only fires after an actual submission
    (see module4/runner.py) and 30s is ample spacing for that.
    """
    raw = os.environ.get("JOBO_APPLY_DELAY_SEC", "").strip()
    return int(raw) if raw.isdigit() else 30


def delay_jitter_seconds() -> int:
    """Extra random 0..N seconds added to the base delay."""
    raw = os.environ.get("JOBO_APPLY_JITTER_SEC", "").strip()
    return int(raw) if raw.isdigit() else 15


def max_jobs_per_run() -> int:
    raw = os.environ.get("JOBO_APPLY_MAX_JOBS", "").strip()
    return int(raw) if raw.isdigit() else 0  # 0 = no cap


def stage_deadline_seconds() -> float:
    """Hard wall-clock cap for the apply loop (seconds). 0 = no deadline.

    Keeps the apply stage bounded so the run finishes, the completion
    marker lands in the log, and the Telegram watchdog can report it.
    Remaining eligible jobs are simply picked up by the next slot.
    """
    raw = os.environ.get("JOBO_APPLY_DEADLINE_SEC", "").strip()
    return float(raw) if raw.replace(".", "", 1).isdigit() and raw not in ("", ".") else 4 * 3600


MAX_ATTEMPTS_PER_JOB = 3


def max_attempts_per_job() -> int:
    """Max apply attempts before a job is permanently dropped (should_apply=0)."""
    raw = os.environ.get("JOBO_APPLY_MAX_ATTEMPTS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else MAX_ATTEMPTS_PER_JOB
