#!/usr/bin/env python
"""The Claude 5-hour session window gate (GHO-22).

On 15 Oct 14:29 four apply workers burned the whole 5-hour session window in
~40 minutes. The weekly gate read 1%, so nothing stopped them; at 15:10 every
agent in flight died on "You've hit your session limit" and the stage quit
with 40 jobs untouched, an hour before its deadline.

Covers:
  * budget_guard: `five_hour` parsing, the session threshold decision, the
    pause-or-stop arithmetic, the usage log line
  * runner: between-job gate pauses until the reset and goes on; stops when
    the reset lands past the deadline; a 429 "session limit" mid-agent pauses
    instead of ending the run; the run stop flag cuts a pause short

Offline: temporary SQLite DB, fake clock, no browser, no model, no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import agent_driver, budget_guard, preflight, runner  # noqa: E402

FAKE_PDF = Path(tempfile.gettempdir()) / "jobo_test_session_resume.pdf"
T0 = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc).timestamp()
JOB_SEC = 7 * 60  # one agent run

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


LIVE_PAYLOAD = {  # trimmed from the live endpoint, 2026-10-15
    "five_hour": {"utilization": 9.0, "resets_at": "2026-10-15T16:29:59.632385+00:00",
                  "limit_dollars": None, "locked_reason": None},
    "seven_day": {"utilization": 1.0, "resets_at": "2026-10-22T10:59:59.632408+00:00"},
    "seven_day_opus": None,
}


def test_parsing():
    print("\n[1] five_hour parsing")
    check("five_hour utilization + resets_at",
          budget_guard.parse_session_payload(LIVE_PAYLOAD) == (0.09, "2026-10-15T16:29:59.632385+00:00", None))
    check("seven_day still parsed the same", budget_guard.parse_usage_payload(LIVE_PAYLOAD)[0] == 0.01)
    util, resets, err = budget_guard.parse_session_payload({"seven_day": {"utilization": 3.0}})
    check("no five_hour -> unreadable, not 0", util is None and resets is None and "five_hour" in (err or ""))
    util, _, _ = budget_guard.parse_session_payload({"five_hour": None})
    check("null five_hour -> unreadable", util is None)

    with ExitStack() as st:
        st.enter_context(patch.object(budget_guard, "read_access_token", return_value="tok"))
        st.enter_context(patch.object(budget_guard, "fetch_usage", return_value=(LIVE_PAYLOAD, None)))
        c = budget_guard.check_budget(threshold=0.75, session_threshold=0.8)
    check("check_budget carries both windows",
          (c.utilization, c.session_utilization, c.session_resets_at)
          == (0.01, 0.09, "2026-10-15T16:29:59.632385+00:00"), str(c))
    with ExitStack() as st:
        st.enter_context(patch.object(budget_guard, "read_access_token", return_value="tok"))
        st.enter_context(patch.object(budget_guard, "fetch_usage", return_value=(None, "usage endpoint unreadable: x")))
        c = budget_guard.check_budget(threshold=0.75, session_threshold=0.8)
    check("endpoint down -> both windows unreadable, nothing stops",
          c.utilization is None and c.session_utilization is None and not c.should_stop and not c.session_limited)

    saved = os.environ.pop("JOBO_SESSION_THRESHOLD", None)
    try:
        check("session threshold default", budget_guard.session_threshold_from_env() == budget_guard.DEFAULT_SESSION_THRESHOLD)
        os.environ["JOBO_SESSION_THRESHOLD"] = "0.6"
        check("JOBO_SESSION_THRESHOLD overrides it", budget_guard.session_threshold_from_env() == 0.6)
        check("weekly threshold untouched by it", budget_guard.threshold_from_env() == budget_guard.DEFAULT_THRESHOLD)
    finally:
        os.environ.pop("JOBO_SESSION_THRESHOLD", None)
        if saved is not None:
            os.environ["JOBO_SESSION_THRESHOLD"] = saved


def test_decision():
    print("\n[2] stop/pause decision")
    d = budget_guard.decide
    check("below session threshold: go", not d(0.01, 0.75, session_utilization=0.79, session_threshold=0.8).session_limited)
    check("at session threshold: limited", d(0.01, 0.75, session_utilization=0.8, session_threshold=0.8).session_limited)
    check("session full does not set the weekly stop",
          not d(0.01, 0.75, session_utilization=1.0, session_threshold=0.8).should_stop)
    check("unreadable session never limits", not d(0.01, 0.75, session_utilization=None).session_limited)
    check("override does not lift the session limit",
          d(0.9, 0.75, override=True, session_utilization=0.95, session_threshold=0.8).session_limited)

    now = T0
    in_30 = iso(now + 1800)
    wait = budget_guard.session_pause_seconds(in_30, now=now, seconds_to_deadline=3 * 3600)
    check("reset in 30 min, 3h left: pause 30 min + slack",
          wait == 1800 + budget_guard.SESSION_RESET_SLACK_SEC, str(wait))
    check("reset after the deadline: stop",
          budget_guard.session_pause_seconds(iso(now + 7200), now=now, seconds_to_deadline=3600) is None)
    check("reset leaves under SESSION_MIN_WORK_SEC: stop",
          budget_guard.session_pause_seconds(in_30, now=now, seconds_to_deadline=1800 + 300) is None)
    check("reset already passed: just the slack",
          budget_guard.session_pause_seconds(iso(now - 5), now=now, seconds_to_deadline=3600)
          == budget_guard.SESSION_RESET_SLACK_SEC)
    check("'Z' timestamps parse",
          budget_guard.session_pause_seconds("2026-10-15T09:30:00Z", now=now, seconds_to_deadline=3 * 3600)
          == 1800 + budget_guard.SESSION_RESET_SLACK_SEC)
    check("unknown reset: stop", budget_guard.session_pause_seconds(None, now=now, seconds_to_deadline=9e9) is None)
    check("garbage reset: stop", budget_guard.session_pause_seconds("5pm", now=now, seconds_to_deadline=9e9) is None)

    c = d(0.03, 0.75, session_utilization=0.45, session_resets_at="2026-10-15T16:29:59+00:00")
    line = budget_guard.usage_summary(c)
    check("usage line shows 5h burn and reset", line.startswith("5h session 45% (resets 2026-10-15T16:29:59+00:00)")
          and "weekly 3%" in line, line)
    check("gate line shows 5h usage", "5h session usage 45%" in budget_guard.gate_message(c, "pipeline"))
    check("gate record carries 5h usage", budget_guard.gate_record(c, "pipeline")["session_utilization"] == 0.45)
    check("unreadable usage line", budget_guard.usage_summary(d(None, 0.75)).startswith("5h session UNREADABLE"))


# --------------------------------------------------------------------------
# runner, fake clock
# --------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = T0
        self.slept = 0.0

    def advance(self, sec: float) -> None:
        self.mono += sec
        self.wall += sec

    def sleep(self, sec: float) -> None:
        self.slept += sec
        self.advance(sec)


def make_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), tmp.name


def add_jobs(session, ids):
    for jid in ids:
        session.add(JobModel(
            id=jid, company_name=f"Co{jid}", job_title="SWE",
            job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
            applied=False, referred_json="[]", metadata_json="{}",
            module2_attempted=True, module2_enriched_at=datetime.now(),
            should_apply=True, module3_fit_score=4,
            module3_resume_pdf_path=str(FAKE_PDF), module3_cover_pdf_path=str(FAKE_PDF),
            module4_attempts=0,
        ))
    session.commit()


SUBMITTED = agent_driver.ApplyOutcome(status="submitted", detail="confirmation page shown")
LIMIT_429 = agent_driver.ApplyOutcome(
    status="stuck",
    detail="agent produced no verdict; last output: Claude Subscription DirectSDK (Experimental) "
           "rate-limited every one of 3 attempts: HTTP 429 You've hit your session limit "
           "· resets 5pm (Asia/Kolkata)")


def run(ids, *, clock, usage, outcomes=None, deadline_sec=4 * 3600):
    """run_module4 over `ids` on a fake clock. usage(clock) -> BudgetCheck."""
    session, db = make_session()
    add_jobs(session, ids)
    spawned: list[int] = []

    def fake_agent(*, job_id, **kw):
        spawned.append(job_id)
        clock.advance(JOB_SEC)
        return (outcomes if outcomes is not None else {}).get(job_id, SUBMITTED)

    open_pf = preflight.PreflightResult(closed=False, reason="open", checked=True)
    try:
        with ExitStack() as st:
            st.enter_context(patch.object(runner, "_monotonic", lambda: clock.mono))
            st.enter_context(patch.object(runner, "_wall", lambda: clock.wall))
            st.enter_context(patch.object(runner, "_sleep", clock.sleep))
            st.enter_context(patch.object(runner.browser_session, "ensure_browser_running", return_value="http://cdp"))
            st.enter_context(patch.object(runner.browser_session, "ensure_healthy", return_value="http://cdp"))
            st.enter_context(patch.object(runner.browser_session, "close_extra_tabs"))
            st.enter_context(patch.object(
                runner.linkedin_session, "check_session",
                return_value=runner.linkedin_session.SessionState("signed_in", "test")))
            st.enter_context(patch.object(runner.preflight, "check_job_open", return_value=open_pf))
            st.enter_context(patch.object(runner.circuit_breaker, "is_tripped", return_value=None))
            st.enter_context(patch.object(runner.circuit_breaker, "record_failure"))
            st.enter_context(patch.object(runner.submission_audit, "write_audit_record"))
            st.enter_context(patch.object(runner.agent_driver, "run_apply_agent", side_effect=fake_agent))
            st.enter_context(patch.object(runner.constants, "apply_delay_seconds", return_value=0))
            st.enter_context(patch.object(runner.constants, "delay_jitter_seconds", return_value=0))
            st.enter_context(patch.object(runner.constants, "max_jobs_per_run", return_value=0))
            st.enter_context(patch.object(runner.constants, "stage_deadline_seconds", return_value=deadline_sec))
            # Stub the attach canary: on its own it launches a real Hermes agent.
            result = runner.run_module4(
                session, budget_check=lambda: usage(clock),
                attach_check=lambda cdp, name: agent_driver.AttachCheck(True, "test"))
        rows = {j.id: j for j in session.query(JobModel).all()}
        return result, spawned, rows
    finally:
        session.close()
        os.unlink(db)


def window(full_until: float, *, high: float = 0.85, low: float = 0.05):
    """A 5h window at `high` until wall time `full_until`, then reset to `low`."""
    def usage(clock):
        full = clock.wall < full_until
        return budget_guard.decide(
            0.02, 0.75,
            session_utilization=high if full else low,
            session_threshold=0.8,
            session_resets_at=iso(full_until) if full else iso(full_until + 5 * 3600),
        )
    return usage


def test_runner_gate():
    print("\n[3] runner: between-job session gate")
    FAKE_PDF.write_bytes(b"%PDF-fake")

    # Window fills during job 1 (T0..T0+7min), resets 20 min after that.
    clock = Clock()
    reset = T0 + JOB_SEC + 20 * 60
    full_from = T0 + 60

    def usage(c):
        if c.wall < full_from:
            return budget_guard.decide(0.02, 0.75, session_utilization=0.5, session_threshold=0.8,
                                       session_resets_at=iso(reset))
        return window(reset)(c)

    result, spawned, rows = run([1, 2, 3, 4], clock=clock, usage=usage)
    check("every job still ran after the pause", spawned == [1, 2, 3, 4], str(spawned))
    check("one pause", result.session_pauses == 1, str(result.session_pauses))
    check("slept until the reset + slack",
          abs(clock.slept - (20 * 60 + budget_guard.SESSION_RESET_SLACK_SEC)) < 1, str(clock.slept))
    check("no session stop", not result.session_limit_stop)
    check("pause is in the details", any(d.startswith("SESSION PAUSE") for d in result.details))
    check("health stays OK on a pause", runner.health_verdict(result).healthy)

    # Window full after job 1, resets 2h later; the stage has 1h left.
    clock = Clock()
    reset = T0 + 2 * 3600

    def usage(c):
        if c.wall < T0 + 60:
            return budget_guard.decide(0.02, 0.75, session_utilization=0.5, session_threshold=0.8)
        return window(reset)(c)

    result, spawned, rows = run([1, 2, 3, 4], clock=clock, usage=usage, deadline_sec=3600)
    check("reset past the deadline: stops before job 2", spawned == [1], str(spawned))
    check("no sleep", clock.slept == 0, str(clock.slept))
    check("stop reason recorded", "too late" in result.session_limit_stop, result.session_limit_stop)
    check("untouched jobs keep their attempts", all((rows[j].module4_attempts or 0) == 0 for j in (2, 3, 4)))
    v = runner.health_verdict(result)
    check("health DEGRADED names the session limit",
          not v.healthy and any("5-hour session" in r for r in v.reasons), str(v.reasons))

    # Already limited at run start, reset with no time to spare: no agent at all.
    clock = Clock()
    result, spawned, _ = run([1, 2], clock=clock, usage=window(T0 + 3 * 3600), deadline_sec=3600)
    check("limited at start + late reset: no agent started", spawned == [], str(spawned))


def test_runner_429():
    print("\n[4] runner: 429 'session limit' mid-agent")
    # Job 2's agent dies on the 429; the endpoint then says full, reset in 30 min.
    clock = Clock()
    reset_holder = {}

    def usage(c):
        if "reset" not in reset_holder:
            return budget_guard.decide(0.02, 0.75, session_utilization=0.6, session_threshold=0.8)
        return window(reset_holder["reset"], high=1.0)(c)

    def agent_429_then_ok(outcomes):
        class Outcomes(dict):
            def get(self, jid, default=None):
                if jid == 2:
                    reset_holder.setdefault("reset", clock.wall + 30 * 60)
                return outcomes.get(jid, default)
        return Outcomes()

    result, spawned, rows = run([1, 2, 3, 4], clock=clock, usage=usage,
                                outcomes=agent_429_then_ok({2: LIMIT_429}))
    check("queue goes on after the reset", spawned == [1, 2, 3, 4], str(spawned))
    check("cut-off job not retried this run", spawned.count(2) == 1)
    check("cut-off job not charged", (rows[2].module4_attempts or 0) == 0 and not rows[2].applied)
    check("it is an infra failure", result.infra_failures == 1, str(result.infra_failures))
    check("one pause, ~30 min", result.session_pauses == 1
          and abs(clock.slept - (30 * 60 + budget_guard.SESSION_RESET_SLACK_SEC)) < 1, str(clock.slept))
    check("no ABORTED line", not any(d.startswith("ABORTED") for d in result.details))

    # Same 429, but the reset is past the deadline: stop as before.
    clock = Clock()
    reset_holder.clear()

    def usage_late(c):
        if "reset" not in reset_holder:
            return budget_guard.decide(0.02, 0.75, session_utilization=0.6, session_threshold=0.8)
        return window(reset_holder["reset"] + 3 * 3600, high=1.0)(c)

    result, spawned, _ = run([1, 2, 3, 4], clock=clock, usage=usage_late, deadline_sec=3600,
                             outcomes=agent_429_then_ok({2: LIMIT_429}))
    check("late reset: stage stops at the hit", spawned == [1, 2], str(spawned))
    check("still reported as a usage-limit stop", any("usage limit" in d for d in result.details))
    check("and as a session stop", bool(result.session_limit_stop))

    # A weekly-limit 429 (weekly stop set) never pauses for the 5h reset.
    clock = Clock()
    weekly = agent_driver.ApplyOutcome(
        status="stuck", detail="agent produced no verdict; Provider said: You've hit your weekly limit")

    def usage_weekly(c):
        return budget_guard.decide(0.5 if c.wall < T0 + 60 else 1.0, 0.75, session_utilization=0.3,
                                   session_threshold=0.8, session_resets_at=iso(T0 + 1800))

    result, spawned, _ = run([1, 2, 3], clock=clock, usage=usage_weekly, outcomes={1: weekly})
    check("weekly 429: stop, no pause", spawned == [1] and result.session_pauses == 0 and clock.slept == 0,
          f"{spawned} pauses={result.session_pauses}")

    # Usage endpoint unreadable after the 429: no reset time, stop as before.
    clock = Clock()
    result, spawned, _ = run([1, 2, 3], clock=clock, usage=lambda c: budget_guard.decide(None, 0.75),
                             outcomes={1: LIMIT_429})
    check("unreadable usage after 429: stop at the first hit", spawned == [1], str(spawned))


def test_stop_flag_cuts_pause():
    print("\n[5] run stop flag ends a pause")
    clock = Clock()
    flags = {"n": 0}

    def stop_reason():
        flags["n"] += 1
        return "li: LinkedIn signed out" if flags["n"] > 2 else None

    worker = SimpleNamespace(queue=SimpleNamespace(stop_reason=stop_reason), stop_all=lambda r: None)
    result = runner.Module4Result()
    with ExitStack() as st:
        st.enter_context(patch.object(runner, "_monotonic", lambda: clock.mono))
        st.enter_context(patch.object(runner, "_wall", lambda: clock.wall))
        st.enter_context(patch.object(runner, "_sleep", clock.sleep))
        ok = runner._pause_for_session_reset(
            iso(T0 + 3600), "test", clock.mono + 4 * 3600, result, worker, lambda: 5)
    check("pause returns False on the stop flag", ok is False)
    check("stopped after two polls, not the full hour",
          clock.slept == 2 * runner.SESSION_PAUSE_POLL_SEC, str(clock.slept))


def test_default_stops_at_session_line():
    print("\n[6] default (no JOBO_SESSION_PAUSE): stop at the session line, never pause")
    saved = os.environ.pop(budget_guard.SESSION_PAUSE_ENV, None)
    try:
        check("default session threshold is 30%", budget_guard.DEFAULT_SESSION_THRESHOLD == 0.30)
        check("pause off by default", budget_guard.session_pause_enabled() is False)
        result = runner.Module4Result()
        now = runner._wall()
        resets = datetime.fromtimestamp(now + 1800, tz=timezone.utc).isoformat()
        ok = runner._pause_for_session_reset(
            resets, "5h session usage 31% >= 30% threshold",
            runner._monotonic() + 6 * 3600, result, None, lambda: 7,
        )
        check("returns False (stop)", ok is False)
        check("no pause counted", result.session_pauses == 0)
        check("stop reason names the user's rule", "the user's rule" in result.session_limit_stop, result.session_limit_stop)
    finally:
        if saved is not None:
            os.environ[budget_guard.SESSION_PAUSE_ENV] = saved


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls main()."""
    print("Claude 5-hour session window gate (GHO-22)")
    test_parsing()
    test_decision()
    # [3]-[5] cover the opt-in pause-and-resume path (JOBO_SESSION_PAUSE=1).
    saved = os.environ.get(budget_guard.SESSION_PAUSE_ENV)
    os.environ[budget_guard.SESSION_PAUSE_ENV] = "1"
    try:
        test_runner_gate()
        test_runner_429()
        test_stop_flag_cuts_pause()
    finally:
        if saved is None:
            os.environ.pop(budget_guard.SESSION_PAUSE_ENV, None)
        else:
            os.environ[budget_guard.SESSION_PAUSE_ENV] = saved
    test_default_stops_at_session_line()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
