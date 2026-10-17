#!/usr/bin/env python
"""Fail fast when apply agents are not on their worker's browser (GHO-17).

What happened (15 Oct 2026, data/logs/run_20261015_1240.log): after the
morning Hermes auto-update every apply agent drove a scratch headless browser
instead of its worker's CDP browser. Agents "saw" LinkedIn signed out (21 in a
row, each forgiven because the feed probe said signed in) and uploads found no
file input; 0 submitted in 19 min, one job dropped. Each feed probe also
killed the LinkedIn browser on CDP 9333 (20 relaunches).

Covers:
  [1] attach_canary: marker found in this browser = pass; elsewhere = fail
      after its retry
  [2] a failed canary stops the stage FAILED before any claim, nothing charged;
      --canary-only claims nothing either
  [3] mismatch breaker: 3 signed-out-but-probe-fine jobs in a row trip it; the
      jobs stay not_attempted with attempts unchanged (even one at 3/3)
  [4] the upload signal trips it too; a normal job in between resets the
      streak; counts from other jobs inside the streak survive
  [5] probe_feed never leaves the browser without a page (fake CDP with
      asynchronous closes, which exits like Chromium at zero pages)

Offline: no model, no real browser. Run via
    bash scripts/run_regression_tests.sh test_attach_canary
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import (  # noqa: E402
    agent_driver,
    apply_queue,
    browser_session,
    budget_guard,
    linkedin_session as ls,
    parallel,
    preflight,
    runner,
)
from module4.outcome_taxonomy import classify_outcome  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="gho17_"))
FAKE_PDF = TMP / "resume.pdf"
Outcome = agent_driver.ApplyOutcome
IN = ls.SessionState("signed_in", "linkedin.com/feed stayed on https://www.linkedin.com/feed/")
SIGNED_OUT_CLAIM = "LinkedIn shows the apply browser as signed out (Sign in/Join now links visible)"

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


# --- [1] the canary itself -----------------------------------------------------


def test_canary_unit():
    print("\n[1] attach_canary")
    prompts: list[str] = []

    def hermes_drives(where: dict):
        def run(*, prompt, cdp_url, **kw):
            prompts.append(prompt)
            marker = prompt.split("with the url ", 1)[1].split()[0]
            where.setdefault(cdp_url, []).append(marker)
            return "CANARY: done", False
        return run

    # Attached: the marker lands in this worker's own browser.
    tabs: dict = {}
    res = agent_driver.attach_canary("http://127.0.0.1:9342", "f1", run_hermes=hermes_drives(tabs),
                                     page_urls=lambda cdp: tuple(tabs.get(cdp, ())))
    check("marker in this browser passes", res.ok, res.detail)
    check("marker is a public URL with a jobo fragment naming the worker",
          prompts and "https://example.com/#jobo-canary-f1-" in prompts[0])

    # The 15 Oct shape: the agent drives some other browser.
    prompts.clear()
    elsewhere: dict = {}

    def scratch(*, prompt, cdp_url, **kw):
        return hermes_drives(elsewhere)(prompt=prompt, cdp_url="http://127.0.0.1:41234")

    res = agent_driver.attach_canary("http://127.0.0.1:9342", "f1", run_hermes=scratch,
                                     page_urls=lambda cdp: ("chrome://newtab/",))
    check("marker elsewhere fails", not res.ok)
    check("failure retried once with a fresh marker", len(prompts) == 2 and prompts[0] != prompts[1])
    check("failure names this browser and what it shows",
          "http://127.0.0.1:9342" in res.detail and "chrome://newtab" in res.detail, res.detail)

    # A dead model is a failure too (no apply agent would work).
    res = agent_driver.attach_canary("http://127.0.0.1:9342", "li",
                                     run_hermes=lambda **kw: ("", True), page_urls=lambda cdp: ())
    check("agent timeout fails", not res.ok and "timed out" in res.detail, res.detail)

    with patch.dict(os.environ, {"JOBO_ATTACH_CANARY": "0"}):
        check("JOBO_ATTACH_CANARY=0 switches it off", not agent_driver.canary_enabled())
    with patch.dict(os.environ, {"JOBO_ATTACH_CANARY": "1"}):
        check("on by default", agent_driver.canary_enabled())


# --- DB / runner helpers -------------------------------------------------------


def make_session(tag: str):
    path = TMP / f"jobs_{tag}.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def add_job(session, jid, *, attempts=0, last_status=None):
    session.add(JobModel(
        id=jid, company_name=f"Co{jid}", job_title="SWE", job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False, referred_json="[]", metadata_json="{}", module2_attempted=True,
        module2_enriched_at=datetime.now(), should_apply=True, module3_fit_score=4,
        module3_resume_pdf_path=str(FAKE_PDF), module3_cover_pdf_path=str(FAKE_PDF),
        module4_attempts=attempts, module4_last_status=last_status,
        module4_outcome=classify_outcome(applied=False, last_status=last_status),
    ))
    session.commit()


def stubs(st: ExitStack, outcomes: dict, spawned: list, *, closed_ids=()):
    def fake_agent(*, job_id, **kw):
        spawned.append(job_id)
        return outcomes[job_id]

    def pf(url):
        jid = int(url.rstrip("/").rsplit("/", 1)[-1])
        if jid in closed_ids:
            return preflight.PreflightResult(closed=True, reason="no longer accepting", checked=True)
        return preflight.PreflightResult(closed=False, reason="open", checked=True)

    st.enter_context(patch.object(runner.browser_session, "ensure_browser_running", return_value="http://cdp"))
    st.enter_context(patch.object(runner.browser_session, "ensure_healthy", return_value="http://cdp"))
    st.enter_context(patch.object(runner.browser_session, "close_extra_tabs"))
    st.enter_context(patch.object(runner.linkedin_session, "check_session", return_value=IN))
    st.enter_context(patch.object(runner.linkedin_session, "confirm_signed_out", return_value=IN))
    st.enter_context(patch.object(runner.google_forms, "page_urls", return_value=[]))
    st.enter_context(patch.object(runner.google_forms, "browser_signed_in", return_value=None))
    st.enter_context(patch.object(runner.preflight, "check_job_open", side_effect=pf))
    st.enter_context(patch.object(runner.circuit_breaker, "is_tripped", return_value=None))
    st.enter_context(patch.object(runner.circuit_breaker, "record_failure"))
    st.enter_context(patch.object(runner.submission_audit, "write_audit_record"))
    st.enter_context(patch.object(runner.agent_driver, "run_apply_agent", side_effect=fake_agent))
    st.enter_context(patch.object(runner.constants, "apply_delay_seconds", return_value=0))
    st.enter_context(patch.object(runner.constants, "delay_jitter_seconds", return_value=0))
    st.enter_context(patch.object(runner.constants, "max_jobs_per_run", return_value=0))


def passing_canary(cdp, name):
    return agent_driver.AttachCheck(True, f"agent drives {cdp}")


def run(session, outcomes, *, closed_ids=(), attach_check=passing_canary, **kw):
    spawned: list = []
    with ExitStack() as st:
        stubs(st, outcomes, spawned, closed_ids=closed_ids)
        result = runner.run_module4(session, budget_check=lambda: budget_guard.decide(None, 1.0),
                                    attach_check=attach_check, **kw)
    return result, spawned


# --- [2] canary in the stage ---------------------------------------------------


def test_canary_gate():
    print("\n[2] a failed canary stops the stage before any claim")
    session = make_session("gate")
    for jid in (1, 2, 3):
        add_job(session, jid)
    q = apply_queue.ApplyQueue("gho17_gate", path=TMP / "queue_gate.sqlite")
    q.create_run([(j, f"Co{j}") for j in (1, 2, 3)])
    worker = parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=0, poll_sec=0.01)
    q.register_worker("li", parallel.ROLE_LINKEDIN, 9333, None)
    seen = []

    def mismatch(cdp, name):
        seen.append((cdp, name))
        return agent_driver.AttachCheck(False, "the agent's browser_navigate never reached it")

    result, spawned = run(session, {}, attach_check=mismatch, worker=worker)
    check("canary ran for this worker", seen == [("http://cdp", "li")], str(seen))
    check("no agent spawned", spawned == [])
    check("no job claimed", all(r["state"] == "pending" for r in q.rows()), str(q.counts()))
    check("run stop flag set for the other workers", bool(q.stop_reason()))
    rows = {j.id: j for j in session.query(JobModel)}
    check("no job touched", all(r.module4_attempts == 0 and r.module4_last_status is None for r in rows.values()))
    verdict = runner.health_verdict(result)
    check("health is FAILED", verdict.label == "FAILED" and not verdict.healthy, verdict.label)
    check("verdict line names the canary and the worker",
          "attach canary FAILED on worker li" in (verdict.reasons or [""])[0], str(verdict.reasons))
    check("nothing counted", result.attempted == 0 and result.failed == 0)
    merged = parallel._merge({"li": parallel.result_to_json(result), "f1": parallel.result_to_json(runner.Module4Result())})
    check("parallel merge keeps the canary failure", runner.health_verdict(merged).label == "FAILED")

    # Single-worker runner, and --canary-only with a passing canary.
    result, spawned = run(session, {}, attach_check=mismatch)
    check("single-worker runner: FAILED, no agent", spawned == [] and runner.health_verdict(result).label == "FAILED")
    result, spawned = run(session, {}, canary_only=True)
    check("--canary-only: canary passes, nothing applied", spawned == [] and runner.health_verdict(result).healthy)
    session.close()


# --- [3]/[4] mismatch breaker --------------------------------------------------


def snapshot(session):
    session.expire_all()
    return {j.id: (j.module4_attempts, j.module4_last_status, j.should_apply, j.module4_outcome, j.applied)
            for j in session.query(JobModel)}


def test_breaker_signout():
    print("\n[3] 3 signed-out-but-probe-fine jobs in a row trip the breaker")
    FAKE_PDF.write_bytes(b"%PDF-fake")
    session = make_session("signout")
    add_job(session, 1629, attempts=2, last_status="stuck:previous failure")  # its 3rd and last attempt
    for jid in (1745, 1753, 1764):
        add_job(session, jid)
    before = snapshot(session)
    outcomes = {j: Outcome(status="linkedin_signed_out", detail=SIGNED_OUT_CLAIM) for j in (1629, 1745, 1753, 1764)}
    result, spawned = run(session, outcomes)
    after = snapshot(session)
    check("stopped after the 3rd mismatch", spawned == [1629, 1745, 1753], str(spawned))
    check("streak jobs exactly as before (attempts, status, should_apply, outcome)",
          all(after[j] == before[j] for j in (1629, 1745, 1753)), f"{before} -> {after}")
    check("fresh jobs stay not_attempted",
          all(after[j][3] == "not_attempted" and after[j][1] is None for j in (1745, 1753)))
    check("the job on its last attempt is not dropped", after[1629][2] is True and after[1629][0] == 2)
    check("4th job untouched", after[1764] == before[1764])
    check("no linkedin_signed_out status left behind",
          not any((after[j][1] or "").startswith("linkedin_signed_out") for j in after))
    verdict = runner.health_verdict(result)
    check("health is DEGRADED with the reason", verdict.label == "DEGRADED"
          and any("not driving this browser" in r and "1629, 1745, 1753" in r for r in verdict.reasons), str(verdict.reasons))
    check("counts taken back out", result.attempted == 0 and result.failed == 0, str(runner._counters(result)))
    check("not reported as a LinkedIn sign-out", not result.linkedin_signed_out)
    session.close()


def test_breaker_upload_and_reset():
    print("\n[4] upload signal, streak reset, other counts kept")
    session = make_session("upload")
    for jid in (1, 2, 3, 4):
        add_job(session, jid)
    before = snapshot(session)
    no_input = dict(status="stuck", detail="resume upload never attached",
                    upload_no_file_input=2, resume_uploads_ok=0)
    outcomes = {j: Outcome(**no_input) for j in (1, 2, 3, 4)}
    result, spawned = run(session, outcomes)
    after = snapshot(session)
    check("upload mismatches trip at 3", spawned == [1, 2, 3], str(spawned))
    check("their attempts are not charged", all(after[j] == before[j] for j in (1, 2, 3)), f"{before} -> {after}")
    check("DEGRADED with the upload reason",
          any("no <input type=file>" in r for r in runner.health_verdict(result).reasons))

    # A failed upload that a later upload in the same job fixed is no mismatch.
    session = make_session("upload_ok")
    for jid in (1, 2, 3):
        add_job(session, jid)
    fixed = dict(no_input, resume_uploads_ok=1)
    result, spawned = run(session, {j: Outcome(**fixed) for j in (1, 2, 3)})
    check("an upload that later succeeded does not count", spawned == [1, 2, 3] and not result.agent_browser_mismatch)

    # mismatch, mismatch, ordinary failure, mismatch, mismatch: never 3 in a row.
    session = make_session("reset")
    for jid in range(1, 6):
        add_job(session, jid)
    outcomes = {j: Outcome(**no_input) for j in (1, 2, 4, 5)}
    outcomes[3] = Outcome(status="stuck", detail="Workday asks for a PAN number")
    result, spawned = run(session, outcomes)
    check("a normal job resets the streak", spawned == [1, 2, 3, 4, 5] and not result.agent_browser_mismatch, str(spawned))
    rows = snapshot(session)
    check("without a trip, failures are charged as before", all(rows[j][0] == 1 for j in range(1, 6)))

    # A preflight-closed job inside the streak keeps its own outcome and count.
    session = make_session("closed_inside")
    for jid in (1, 2, 3, 4):
        add_job(session, jid)
    result, spawned = run(session, {j: Outcome(**no_input) for j in (1, 3, 4)}, closed_ids=(2,))
    rows = snapshot(session)
    check("closed job in between does not break the streak", spawned == [1, 3, 4], str(spawned))
    check("its closed outcome and count survive the trip",
          rows[2][2] is False and result.closed == 1 and result.preflight_closed == 1, str(runner._counters(result)))
    check("streak jobs restored", all(rows[j][0] == 0 and rows[j][1] is None for j in (1, 3, 4)))


# --- [5] probe_feed against a fake CDP -----------------------------------------


class FakeCDP:
    """/json/{list,new,close} with Chromium's two relevant habits: a close
    lands a moment later (the target stays listed meanwhile), newest tab is
    listed first, and the process exits once no page is left."""

    CLOSE_DELAY = 0.6

    def __init__(self, urls):
        self.pages = [{"id": f"T{i}", "type": "page", "url": u} for i, u in enumerate(urls)]
        self.closing: dict[str, float] = {}
        self.alive = True
        self.n = 100
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.handle(self)

            do_PUT = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _settle(self):
        now = time.monotonic()
        gone = {i for i, t in self.closing.items() if t <= now}
        if gone:
            self.pages = [p for p in self.pages if p["id"] not in gone]
            for i in gone:
                del self.closing[i]
            if not self.pages:
                self.alive = False  # Chromium exits with its last page

    def handle(self, h):
        with self.lock:
            self._settle()
            if not self.alive:
                h.send_response(502)
                h.end_headers()
                return
            path = h.path
            body: object
            if path.startswith("/json/list"):
                body = list(self.pages)
            elif path.startswith("/json/new"):
                self.n += 1
                tab = {"id": f"T{self.n}", "type": "page", "url": unquote(path.split("?", 1)[1]) if "?" in path else "about:blank"}
                self.pages.insert(0, tab)
                body = tab
            elif path.startswith("/json/close/"):
                self.closing[path.rsplit("/", 1)[-1]] = time.monotonic() + self.CLOSE_DELAY
                body = "Target is closing"
            else:
                body = {}
            data = (json.dumps(body) if not isinstance(body, str) else body).encode()
            h.send_response(200)
            h.end_headers()
            h.wfile.write(data)

    def page_count(self):
        with self.lock:
            self._settle()
            return len(self.pages) if self.alive else 0

    def stop(self):
        self.server.shutdown()


def test_probe_keeps_a_page():
    print("\n[5] probe_feed never closes the last page")
    # The runner's sequence on 15 Oct: probe, then close_extra_tabs right away.
    fake = FakeCDP(["about:blank"])
    try:
        with patch.object(browser_session, "cdp_port", return_value=fake.port):
            state = ls.probe_feed(fake.base, settle_sec=1.0)
            browser_session.close_extra_tabs()
        time.sleep(FakeCDP.CLOSE_DELAY + 0.3)
        check("probe still answers", state.signed_in, f"{state.state}: {state.reason}")
        check("browser alive with a page after probe + tab cleanup", fake.alive and fake.page_count() >= 1,
              f"alive={fake.alive} pages={fake.pages}")
    finally:
        fake.stop()

    # The fake does reproduce the 15 Oct death: an unwaited close, then cleanup.
    fake = FakeCDP(["about:blank"])
    try:
        with patch.object(browser_session, "cdp_port", return_value=fake.port):
            tab = ls._http_json(f"{fake.base}/json/new?{ls.PROBE_URL}", method="PUT")
            ls._http_json(f"{fake.base}/json/list")
            import urllib.request
            urllib.request.urlopen(f"{fake.base}/json/close/{tab['id']}", timeout=3).read()
            browser_session.close_extra_tabs()
        time.sleep(FakeCDP.CLOSE_DELAY + 0.3)
        check("(control) the old unwaited close kills the fake browser", not fake.alive)
    finally:
        fake.stop()

    # No page at all to begin with: a guard is opened before the probe tab.
    fake = FakeCDP([])
    try:
        state = ls.probe_feed(fake.base, settle_sec=0.6)
        time.sleep(FakeCDP.CLOSE_DELAY + 0.3)
        check("with no page, a guard tab keeps the browser up", fake.alive and fake.page_count() == 1,
              f"alive={fake.alive} pages={fake.pages}")
    finally:
        fake.stop()


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls main()."""
    print("Attach canary + mismatch breaker + safe LinkedIn probe (GHO-17)")
    FAKE_PDF.write_bytes(b"%PDF-fake")
    test_canary_unit()
    test_canary_gate()
    test_breaker_signout()
    test_breaker_upload_and_reset()
    test_probe_keeps_a_page()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
