#!/usr/bin/env python
"""A site's application cap parks the host; a site down on two runs closes (GHO-110).

2371, 2026-10-30 22:53: amazon.jobs answered "You've reached the maximum
number of job applications." (?result=application_limit_reach) right after
the run's third Amazon submission. The agent called it stuck and an attempt
was charged, though no retry before the cap lifts can change anything. Now
the host is parked for its cool-down (module4/application_caps.py), other jobs
routed there wait without an agent, and the retry after the cool-down gets no
RETRY NOTICE quoting the refusal.

2451, 25 Oct and 30 Oct: the employer's job page printed "Failed to connect
to <backend> port 443" on both runs. The second sighting closes it as a dead
site (module4/blockers.py) instead of spending the third attempt.

Offline: temporary SQLite DB, no browser, no model calls.

    uv run python scripts/test_application_caps.py
"""
from __future__ import annotations

import datetime
import sys
import tempfile
from contextlib import ExitStack
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import agent_driver, application_caps, blockers, budget_guard, query, run_report, runner  # noqa: E402

AMAZON = "www.amazon.jobs"
OTHER = "boards.greenhouse.io"
# 2371's verdict, word for word.
CAP_DETAIL = ("Amazon.jobs blocked the application with \"You've reached the maximum number of job "
              "applications\" when replacing the stale resume from job 2352")
# 2451's two verdicts (25 Oct, 30 Oct), employer hosts replaced.
DOWN_1 = ("contoso.example career/apply page fails to load, server error \"Failed to connect to "
          "fabrikam-backend.example port 443 after 134389 ms: Could not connect to server\" on repeated "
          "attempts — the application form never renders")
DOWN_2 = ("contoso.example career/job-details page fails to render on repeated loads with \"Failed to "
          "connect to fabrikam-backend.example port 443: Could not connect to server\" — no application "
          "form or apply control ever appears")

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
    tmp = tmp or Path(tempfile.mkdtemp(prefix="gho110_"))
    engine = create_engine(f"sqlite:///{tmp}/jobs.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(s, jid, url, *, attempts=0, last=None):
    pdf = Path(tempfile.gettempdir()) / "jobo_app_cap_resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    j = JobModel(id=jid, company_name="Contoso", job_title="Software Engineer", job_url=url,
                 should_apply=True, applied=False, module2_enriched_at=datetime.datetime.now(),
                 module3_resume_pdf_path=str(pdf), module4_attempts=attempts, module4_last_status=last)
    s.add(j)
    s.commit()
    return j


def _run(s, outcome, **kw):
    spawned, prompts = [], {}

    def fake_agent(**k):
        spawned.append(k["job_id"])
        prompts[k["job_id"]] = k["user_prompt"]
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
    return res, spawned, prompts


def test_rules():
    print("\n[1] the cap message, the status and the cool-down")
    check("2371's verdict is a cap on the form's host",
          application_caps.cap_host(CAP_DETAIL, f"https://{AMAZON}/en-gb/jobs/10000001/sde") == AMAZON)
    check("from a LinkedIn listing: the host the agent names",
          application_caps.cap_host(CAP_DETAIL, "https://www.linkedin.com/jobs/view/1/") == "amazon.jobs")
    check("the page URL's marker counts too",
          application_caps.cap_host("redirected to /summary?result=application_limit_reach", f"https://{AMAZON}/x") == AMAZON)
    check("LinkedIn's own limit never parks linkedin.com",
          application_caps.cap_host("You have reached the daily application limit", "https://www.linkedin.com/jobs/view/1/") is None)
    check("an ordinary stuck verdict is no cap",
          application_caps.cap_host("Submit did nothing after the resume upload", f"https://{AMAZON}/x") is None)
    now = datetime.datetime(2026, 10, 30, 17, 24, tzinfo=timezone.utc)
    end = application_caps.cooldown_end(AMAZON, now)
    check("amazon.jobs cools down for a week", end == now + timedelta(days=7), str(end))
    check("other hosts for a day", application_caps.cooldown_end(OTHER, now) == now + timedelta(days=1))
    st = application_caps.status_for(AMAZON, end, CAP_DETAIL)
    check("status round-trips", application_caps.parked_until(st) == (AMAZON, end.replace(second=0)), st)
    check("parked during the cool-down", application_caps.is_parked(st, now + timedelta(days=6)))
    check("released after it, with no DB change", not application_caps.is_parked(st, now + timedelta(days=8)))
    check("run-report files it as site:application_cap (temporary)",
          run_report.signature_for(st) == "site:application_cap"
          and run_report.suggested_bucket("site:application_cap") == "temporary", run_report.signature_for(st))
    check("2371's stored verdict too", run_report.signature_for(f"stuck:{CAP_DETAIL}") == "site:application_cap")


def test_runner_cap():
    print("\n[2] 2371: a cap refusal parks the job, charges nothing")
    s = _session()
    j = _job(s, 2371, f"https://{AMAZON}/en-gb/jobs/10000001/sde-i")
    res, spawned, _ = _run(s, agent_driver.ApplyOutcome("stuck", CAP_DETAIL))
    s.refresh(j)
    check("agent ran once", spawned == [2371], str(spawned))
    check("no attempt charged", not j.module4_attempts, str(j.module4_attempts))
    check("still should_apply, not closed", j.should_apply is True and j.module4_closed_at is None)
    parked = application_caps.parked_until(j.module4_last_status)
    check("parked on amazon.jobs for about a week",
          bool(parked) and parked[0] == AMAZON
          and timedelta(days=6, hours=23) < parked[1] - datetime.datetime.now(timezone.utc) <= timedelta(days=7),
          j.module4_last_status)
    check("counted as paused, not failed", res.paused == 1 and res.failed == 0 and res.dropped == 0)

    print("\n[3] during the cool-down: no agent for any amazon.jobs job")
    j2 = _job(s, 2650, f"https://{AMAZON}/en/jobs/10000002/sde-ii")
    j3 = _job(s, 2651, f"https://{OTHER}/contoso/jobs/1")
    res, spawned, _ = _run(s, agent_driver.ApplyOutcome("submitted", "submitted"))
    s.refresh(j2)
    check("the parked job is not queued", 2371 not in [r.id for r in query.list_jobs_to_apply(s)])
    check("only the other host's job got an agent", spawned == [2651], str(spawned))
    check("the second Amazon job parked until the same end, uncharged",
          application_caps.parked_until(j2.module4_last_status) == parked and not j2.module4_attempts,
          j2.module4_last_status)
    check("--job-id still reaches a parked job", [r.id for r in query.list_jobs_to_apply(s, job_id=2371)] == [2371])

    print("\n[4] after the cool-down: tried again, without a notice quoting the refusal")
    s2 = _session()
    old = application_caps.status_for(AMAZON, datetime.datetime.now(timezone.utc) - timedelta(hours=1), CAP_DETAIL)
    j4 = _job(s2, 2372, f"https://{AMAZON}/en/jobs/10000003/sde", attempts=1, last=old)
    res, spawned, prompts = _run(s2, agent_driver.ApplyOutcome("submitted", "submitted"))
    s2.refresh(j4)
    check("queued and applied", spawned == [2372] and j4.applied, str(spawned))
    check("no RETRY NOTICE about the cap",
          "maximum number" not in prompts.get(2372, "") and "RETRY NOTICE" not in prompts.get(2372, ""))

    print("\n[5] single worker: a LinkedIn listing whose Apply hit the cap parks amazon.jobs, not LinkedIn")
    s3 = _session()
    j5 = _job(s3, 2373, "https://www.linkedin.com/jobs/view/2373/")
    _run(s3, agent_driver.ApplyOutcome("stuck", CAP_DETAIL))
    s3.refresh(j5)
    check("parked on amazon.jobs, uncharged",
          (application_caps.parked_until(j5.module4_last_status) or ("",))[0] == "amazon.jobs"
          and not j5.module4_attempts, j5.module4_last_status)
    _job(s3, 2374, "https://www.linkedin.com/jobs/view/2374/")
    _, spawned, _ = _run(s3, agent_driver.ApplyOutcome("submitted", "submitted"))
    check("the next LinkedIn job still runs", spawned == [2374], str(spawned))


def test_router_and_form_worker():
    print("\n[6] parallel: the router parks a listing applying on a capped host; a form worker too")
    from module4 import apply_queue, linkedin_router, parallel

    tmp = Path(tempfile.mkdtemp(prefix="gho110_"))
    s = _session(tmp)
    until = datetime.datetime.now(timezone.utc) + timedelta(days=3)
    _job(s, 2352, f"https://{AMAZON}/en/jobs/10000004/sde", last=application_caps.status_for(AMAZON, until, CAP_DETAIL))
    j = _job(s, 2345, "https://www.linkedin.com/jobs/view/2345/")
    q = apply_queue.ApplyQueue("test_gho110_router", path=tmp / "queue.sqlite")
    q.create_run([(2345, "Contoso")])
    li = parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=1, poll_sec=0.01)
    q.register_worker("f1", parallel.ROLE_FORM, None, None)
    route = linkedin_router.Route(linkedin_router.EXTERNAL, f"https://{AMAZON}/en/jobs/10000005/sde", "ext")

    def resolve(cdp, url):
        q.worker_done("f1")
        return route

    with patch.object(linkedin_router, "resolve", side_effect=resolve):
        res, spawned, _ = _run(s, agent_driver.ApplyOutcome("submitted", "submitted"), worker=li)
    s.refresh(j)
    check("router: no agent", spawned == [], str(spawned))
    check("router: parked until the running cool-down ends, uncharged",
          (application_caps.parked_until(j.module4_last_status) or ("", None))[1] == until.replace(second=0, microsecond=0)
          and not j.module4_attempts, j.module4_last_status)
    check("router: not handed to a form worker", q.pending(apply_queue.ROUTE_FORM) == 0 and res.routed_form == 0)

    # A job routed before the cap was hit reaches a form worker afterwards.
    tmp2 = Path(tempfile.mkdtemp(prefix="gho110_"))
    s2 = _session(tmp2)
    _job(s2, 2352, f"https://{AMAZON}/en/jobs/10000004/sde", last=application_caps.status_for(AMAZON, until, CAP_DETAIL))
    j2 = _job(s2, 3331, "https://www.linkedin.com/jobs/view/3331/")
    q2 = apply_queue.ApplyQueue("test_gho110_form", path=tmp2 / "queue.sqlite")
    q2.create_run([(3331, "Contoso")])
    q2.register_worker("li", parallel.ROLE_LINKEDIN, None, None)
    q2.set_route(3331, apply_queue.ROUTE_FORM, f"https://{AMAZON}/en/jobs/10000006/sde", "external: test")
    q2.worker_done("li")
    f1 = parallel.Worker("f1", parallel.ROLE_FORM, q2, form_workers=1, poll_sec=0.01)
    res, spawned, _ = _run(s2, agent_driver.ApplyOutcome("submitted", "submitted"), worker=f1)
    s2.refresh(j2)
    check("form worker: no agent on the capped host", spawned == [], str(spawned))
    check("form worker: parked, uncharged",
          application_caps.is_parked(j2.module4_last_status) and not j2.module4_attempts, j2.module4_last_status)


def test_dead_site():
    print("\n[7] 2451: an employer site unreachable on two runs closes; once is a retry")
    s = _session()
    j = _job(s, 2451, "https://www.linkedin.com/jobs/view/2451/")
    _run(s, agent_driver.ApplyOutcome("stuck", DOWN_1))
    s.refresh(j)
    check("first run: an ordinary charged failure, still queued",
          j.module4_attempts == 1 and j.should_apply is True and j.module4_closed_at is None, j.module4_last_status)
    res, spawned, _ = _run(s, agent_driver.ApplyOutcome("stuck", DOWN_2))
    s.refresh(j)
    check("second run: closed as a dead site",
          spawned == [2451] and j.module4_closed_at is not None
          and (j.module4_last_status or "").startswith("closed:employer site unreachable on two runs"),
          j.module4_last_status)
    check("counted closed, not failed", res.closed == 1 and res.failed == 0)

    check("our CDP endpoint refusing twice is never a dead site",
          blockers.dead_listing_reason("stuck", "connection refused at 127.0.0.1:9333, could not load the job page",
                                       previous="stuck:connection refused at 127.0.0.1:9333") is None)
    check("LinkedIn unreachable twice is our network, not the employer",
          blockers.dead_listing_reason("stuck", "ERR_NAME_NOT_RESOLVED at https://www.linkedin.com/jobs/view/1/",
                                       previous="stuck:ERR_NAME_NOT_RESOLVED at https://www.linkedin.com/jobs/view/1/")
          is None)
    check("two different sites down is no repeat",
          blockers.dead_listing_reason("stuck", "ERR_CONNECTION_REFUSED at careers.fabrikam.example",
                                       previous="stuck:ERR_CONNECTION_REFUSED at jobs.contoso.example") is None)
    check("the last run's failure was something else",
          blockers.dead_listing_reason("stuck", DOWN_2, previous="stuck:Submit did nothing at contoso.example") is None)
    check("no stored failure (first try)", blockers.dead_listing_reason("stuck", DOWN_2) is None)


def main() -> int:
    test_rules()
    test_runner_cap()
    test_router_and_form_worker()
    test_dead_site()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
