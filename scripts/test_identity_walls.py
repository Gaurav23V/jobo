#!/usr/bin/env python
"""Apple's account wall is dropped before any agent, and no personal data is made up (GHO-92).

2095 (Apple, SRE), 2026-10-24 22:00 run: the LinkedIn Apply link opened
jobs.apple.com, which shows no form until the visitor signs in with an Apple
Account. The agent spent all 80 model steps creating one inside nested
cross-origin iframes, and typed a birthday it called a "placeholder DOB, not
a candidate fact" into Apple's signup. Every Apple job before it ended with
the same external-identity drop (611: trusted-device 2FA; 1854: reset
captcha). So:

  * a job_url or a routed Apply link on jobs.apple.com is dropped with that
    reason before any agent runs (module4/identity_walls.py);
  * the prompt forbids typing a date of birth or any other personal detail
    the facts do not give, on account forms too, and says how to stop.

Offline: temporary SQLite DB, no browser, no model calls.

    uv run python scripts/test_identity_walls.py
"""
from __future__ import annotations

import datetime
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import (  # noqa: E402
    agent_driver, blockers, budget_guard, identity_walls, prompts, run_report, runner,
)

APPLE = "jobs.apple.com"
APPLE_JOB = f"https://{APPLE}/en-us/details/200000000-0000/site-reliability-engineer?board_id=17682"
EXTERNAL_IDENTITY = "sign-in only via an external identity (Google/SSO/Apple ID/2FA device)"

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


def _session(tmp: Path | None = None):
    tmp = tmp or Path(tempfile.mkdtemp(prefix="gho92_"))
    engine = create_engine(f"sqlite:///{tmp}/jobs.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(s, jid, url):
    pdf = Path(tempfile.gettempdir()) / "jobo_identity_wall_resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    j = JobModel(id=jid, company_name="Contoso", job_title="Site Reliability Engineer", job_url=url,
                 should_apply=True, applied=False, module2_enriched_at=datetime.datetime.now(),
                 module3_resume_pdf_path=str(pdf), module4_attempts=0)
    s.add(j)
    s.commit()
    return j


def _run(s, outcome, **kw):
    spawned = []

    def fake_agent(**k):
        spawned.append(k["job_id"])
        return outcome

    with ExitStack() as st:
        st.enter_context(patch.object(runner.browser_session, "ensure_browser_running", return_value="http://127.0.0.1:1"))
        st.enter_context(patch.object(runner.browser_session, "ensure_healthy", return_value="http://127.0.0.1:1"))
        st.enter_context(patch.object(runner.browser_session, "close_extra_tabs"))
        st.enter_context(patch.object(runner.preflight, "check_job_open", return_value=SimpleNamespace(should_skip=False)))
        st.enter_context(patch.object(runner.agent_driver, "run_apply_agent", side_effect=fake_agent))
        st.enter_context(patch.object(runner.submission_audit, "write_audit_record"))
        st.enter_context(patch.object(runner.circuit_breaker, "is_tripped", return_value=None))
        st.enter_context(patch.object(runner.circuit_breaker, "record_failure"))
        st.enter_context(patch.object(runner.linkedin_session, "check_session",
                                      return_value=runner.linkedin_session.SessionState("signed_in", "test")))
        st.enter_context(patch.object(runner.time, "sleep"))
        # Never the real attach canary: it would start a Hermes agent.
        res = runner.run_module4(s, budget_check=lambda: budget_guard.decide(None, 1.0),
                                 attach_check=lambda *a: agent_driver.AttachCheck(True, "test"), **kw)
    return res, spawned


def test_hosts_and_reason():
    print("\n[1] jobs.apple.com is an identity wall; its status drops with the Apple reason")
    check("an Apple job page", identity_walls.identity_wall_for_url(APPLE_JOB) == APPLE)
    check("LinkedIn and other hosts are not",
          identity_walls.identity_wall_for_url("https://www.linkedin.com/jobs/view/2095/") is None
          and identity_walls.identity_wall_for_url("https://boards.greenhouse.io/contoso/jobs/1") is None)
    check("look-alikes are not", identity_walls.identity_wall_for_url("https://notjobs.apple.com.example/x") is None
          and identity_walls.identity_wall_for_url("https://myjobs.apple.company/x") is None)
    check("junk input is not", identity_walls.identity_wall_for_url(None) is None
          and identity_walls.identity_wall_for_url("http://[::1") is None)
    status = identity_walls.status_for(APPLE)
    check("status is a stuck verdict naming the host", status.startswith(f"stuck:{APPLE} "), status)
    check("blockers drop it with the external-identity reason",
          blockers.permanent_reason_from_last_status(status) == EXTERNAL_IDENTITY,
          str(blockers.permanent_reason_from_last_status(status)))
    check("run-report files it under signin:oauth_only (Apple's ledger row), not personal:id_or_dob",
          run_report.signature_for(status) == "signin:oauth_only", run_report.signature_for(status))


def test_runner_job_url():
    print("\n[2] a job_url on jobs.apple.com is dropped before any agent, uncharged")
    s = _session()
    j_apple = _job(s, 2593, APPLE_JOB)
    j_li = _job(s, 2594, "https://www.linkedin.com/jobs/view/2594/")
    res, spawned = _run(s, agent_driver.ApplyOutcome("submitted", "submitted"))
    s.refresh(j_apple)
    s.refresh(j_li)
    check("no agent for the Apple job; the LinkedIn job runs", spawned == [2594], str(spawned))
    check("dropped, no attempt charged",
          j_apple.should_apply is False and not j_apple.module4_attempts, f"{j_apple.should_apply} {j_apple.module4_attempts}")
    check("status names the host", (j_apple.module4_last_status or "").startswith(f"stuck:{APPLE} "),
          j_apple.module4_last_status)
    check("counted as a permanent drop", res.dropped == 1 and res.permanent_blocked == 1 and res.failed == 0,
          f"{res.dropped} {res.permanent_blocked} {res.failed}")
    check("run detail says why", any(f"DROPPED before any agent, {APPLE}" in d for d in res.details), str(res.details))

    s2 = _session()
    _job(s2, 2595, APPLE_JOB)
    _, spawned = _run(s2, agent_driver.ApplyOutcome("submitted", "submitted"), force=True)
    check("--force still reaches it, for testing", spawned == [2595], str(spawned))


def test_router_drop():
    print("\n[3] 2095: a LinkedIn listing whose Apply link opens jobs.apple.com gets no form agent")
    from module4 import apply_queue, linkedin_router, parallel

    tmp = Path(tempfile.mkdtemp(prefix="gho92_router_"))
    s = _session(tmp)
    j = _job(s, 2095, "https://www.linkedin.com/jobs/view/2095/")
    q = apply_queue.ApplyQueue("test_gho92", path=tmp / "queue.sqlite")
    q.create_run([(2095, "Contoso")])
    li = parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=1, poll_sec=0.01)
    q.register_worker("f1", parallel.ROLE_FORM, None, None)  # a live form worker, so the LinkedIn worker routes
    route = linkedin_router.Route(linkedin_router.EXTERNAL, APPLE_JOB, f"Apply opened {APPLE}")

    def resolve(cdp, url):
        q.worker_done("f1")  # then the LinkedIn worker stops once nothing is left
        return route

    with patch.object(linkedin_router, "resolve", side_effect=resolve):
        res, spawned = _run(s, agent_driver.ApplyOutcome("submitted", "submitted"), worker=li)
    s.refresh(j)
    check("no agent ran", spawned == [], str(spawned))
    check("not handed to a form worker", q.pending(apply_queue.ROUTE_FORM) == 0 and res.routed_form == 0)
    check("dropped with the Apple reason, uncharged",
          j.should_apply is False and not j.module4_attempts
          and blockers.permanent_reason_from_last_status(j.module4_last_status) == EXTERNAL_IDENTITY,
          f"{j.should_apply} {j.module4_attempts} {j.module4_last_status}")
    check("counted as a permanent drop, not an attempt",
          res.permanent_blocked == 1 and res.attempted == 0, f"{res.permanent_blocked} {res.attempted}")


def test_prompt():
    print("\n[4] the prompt: no made-up personal data on any form, and stop on Apple's wall")
    p = prompts.SYSTEM_PROMPT
    rule = p[p.index("NEVER type personal data"):][:700]
    check("names date of birth", "date of" in rule and "birth" in rule, rule[:200])
    check("covers account and registration forms", "account and" in rule and "registration forms" in rule, rule[:400])
    check("forbids a placeholder value", "placeholder" in rule)
    check("says to stop with a stuck verdict", "STATUS: stuck" in rule)
    dob = "DETAIL: jobs.apple.com account signup requires date of birth, which is not in the candidate facts"
    check("that verdict is dropped as missing personal data",
          blockers.permanent_reason("stuck", dob) == "form requires personal data that is not in the candidate facts")
    check("and filed under personal:id_or_dob", run_report.signature_for(f"stuck:{dob}") == "personal:id_or_dob")

    check("the Apple stop names jobs.apple.com", f"**{APPLE}** shows no" in p.replace("\n", " "))
    section = p[p.index("Apple Account walls"):][:800]
    check("tells the agent not to create an Apple Account", "do not" in section and "create an Apple Account" in section)
    apple = "jobs.apple.com requires an Apple ID sign-in (trusted device); not attempted"
    check("the Apple verdict it asks for drops with the external-identity reason",
          blockers.permanent_reason("stuck", apple) == EXTERNAL_IDENTITY)
    check("no unformatted placeholder left", "{IDENTITY_WALLS_TEXT}" not in p)


def main() -> int:
    test_hosts_and_reason()
    test_runner_job_url()
    test_router_drop()
    test_prompt()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
