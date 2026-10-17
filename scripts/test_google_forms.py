#!/usr/bin/env python
"""Google Form sign-in walls are parked, not dropped (module4/google_forms.py).

Offline: temporary SQLite DB, fake CDP answers, no browser, no model calls.

    uv run python scripts/test_google_forms.py

With --live it also checks the three real forms on record (HTTP, signed out)
and, if JOBO_GFORMS_TEST_CDP points at a SCRATCH browser (never 9333), that
the browser is seen as signed out and a real form lands on the sign-in page.
"""
from __future__ import annotations

import os
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
from module4 import agent_driver, blockers, budget_guard, google_forms, runner  # noqa: E402

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


# Verbatim module4_last_status DETAILs of the five dropped jobs.
REAL_DETAILS = {
    233: "Application redirects to a Google Form that requires Google account sign-in, and no Google credentials/session are available to pass the login wall.",
    286: "The company's application is a Google Form restricted to signed-in Google accounts, and no Google login credentials are available to pass the sign-in",
    841: "Apply flow routes to a Google Form requiring Google Account sign-in only, no email/password option available; confirmed again on retry, not retryable.",
    1368: "Apply flow leads to a Google Form that requires signing into a Google Account first; no Google credentials are available and there's no alternative sign-in or guest path to access/fill the form.",
    1379: "Apply link leads to a Google Form that requires signing into a Google Account before any form fields are visible; no credentials available and no alternative access path.",
    # GHO-14: dropped after the parking fix existed. 1489 put two bare links
    # (~150 chars) between "Google Form" and "sign-in".
    1059: "Apply flow redirects to a Google Form that requires signing in with a Google Account first (no email/password or anonymous-access alternative), so the application form itself is unreachable.",
    1489: "The Google Form (forms.gle/Ex4mpleF0rmL1nk00 \u2192 docs.google.com/forms/d/e/1FAIpQLSExampleForm04_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform) requires Google sign-in to access, and no email/password alternative apply path exists on the Lamna careers page.",
}
# Must stay what they were: not Google Form walls.
OTHER_DETAILS = [
    ("Microsoft's Eightfold careers site only offers OAuth sign-up (Microsoft, LinkedIn, Google, Facebook), no email/password option", True),
    ("Google Careers apply flow forces sign-in to a Google Account with no email/password ATS option; account creation requires phone/SMS verification", None),
    # Retryable on main too (no "only offers google" phrase): unchanged.
    ("Workable portal only offers Continue with Google sign-in, no email/password option", None),
    ("Lever careers site is SSO-only via Google Workspace", True),
    ("Google Form fully filled but the Submit click did not register", None),
    ("ran out of step budget (80 model steps) mid-form, before submitting", None),
    # What Google shows a signed-in outsider on a Workspace-only form.
    ("Google Form says 'You need permission. This form can only be viewed by users in the owner's organisation.'", True),
]

REAL_FORMS = {
    841: "https://docs.google.com/forms/d/e/1FAIpQLSExampleForm02_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform",
    1368: "https://docs.google.com/forms/d/e/1FAIpQLSExampleForm03_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform",
    1379: "https://docs.google.com/forms/d/e/1FAIpQLSExampleForm01_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform",
}
SIGNIN_TAB = (
    "https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fdocs.google.com%2Fforms%2Fd%2Fe%2F"
    "1FAIpQLSExampleForm01_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx%2Fviewform&followup=x&ltmpl=forms&service=wise"
)


def test_detection():
    print("\n[1] detection on the real DETAIL lines")
    for jid, d in REAL_DETAILS.items():
        check(f"job {jid}: seen as a Google Form sign-in wall", google_forms.is_signin_wall(d))
        check(f"job {jid}: no longer permanent", blockers.permanent_reason("stuck", d) is None,
              repr(blockers.permanent_reason("stuck", d)))
    for d, permanent in OTHER_DETAILS:
        check(f"not a form wall: {d[:50]}...", not google_forms.is_signin_wall(d))
        got = blockers.permanent_reason("stuck", d)
        check(f"  permanence unchanged ({'permanent' if permanent else 'retryable'})",
              (got is not None) == bool(permanent), repr(got))
    check("sign-in tab with a form continue= is a wall", google_forms.is_signin_wall("", [SIGNIN_TAB]))
    check("plain Google sign-in tab is not", not google_forms.is_signin_wall(
        "", ["https://accounts.google.com/v3/signin/identifier?continue=https://mail.google.com"]))
    check("form URL pulled out of the sign-in URL",
          google_forms.find_form_url(SIGNIN_TAB) == REAL_FORMS[1379], google_forms.find_form_url(SIGNIN_TAB))
    check("form URL normalised to /viewform",
          google_forms.find_form_url(REAL_FORMS[841].replace("/viewform", "/formResponse?x=1")) == REAL_FORMS[841])
    check("forms.gle link kept", google_forms.find_form_url("see https://forms.gle/AbC123xyz") == "https://forms.gle/AbC123xyz")
    check("bare forms.gle link gets its scheme back (job 1489)",
          google_forms.find_form_url(REAL_DETAILS[1489]) == "https://forms.gle/Ex4mpleF0rmL1nk00",
          google_forms.find_form_url(REAL_DETAILS[1489]))
    check("bare docs.google.com form link gets its scheme back",
          google_forms.find_form_url("form at " + REAL_FORMS[1368][len("https://"):]) == REAL_FORMS[1368])
    check("a URL-only DETAIL still reads as a form wall",
          google_forms.is_signin_wall(REAL_FORMS[1368] + " requires a Google sign-in before any field shows"))
    check("an unrelated link plus 'sign in' is not a form wall",
          not google_forms.is_signin_wall("careers.example.com/jobs/123 requires sign in with Google"))
    st = google_forms.status_for(REAL_FORMS[841], "Google Form requires Google sign-in")
    check("parked status round-trips the URL", google_forms.parked_form_url(st) == REAL_FORMS[841])
    check("parked status is not permanent", blockers.permanent_reason_from_last_status(st) is None)
    check("parked status is recognised", google_forms.is_parked(st) and not google_forms.is_parked("stuck:x"))


def _session():
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(s, jid, *, attempts=0, last=None):
    pdf = Path(tempfile.gettempdir()) / "jobo_gforms_resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    j = JobModel(id=jid, company_name="Corvid", job_title="Full Stack AI Engineer",
                 job_url=f"https://www.linkedin.com/jobs/view/{jid}/", should_apply=True, applied=False,
                 module2_enriched_at=__import__("datetime").datetime.now(),
                 module3_resume_pdf_path=str(pdf), module4_attempts=attempts, module4_last_status=last)
    s.add(j)
    s.commit()
    return j


def _run(s, *, outcome=None, signed_in=False, tabs=(), form_open=False):
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
        # The runner also checks the LinkedIn session before each job
        # (module4/linkedin_session.py); keep it offline and signed in here.
        st.enter_context(patch.object(runner.linkedin_session, "check_session",
                                      return_value=runner.linkedin_session.SessionState("signed_in", "test")))
        st.enter_context(patch.object(google_forms, "browser_signed_in", return_value=signed_in))
        st.enter_context(patch.object(google_forms, "page_urls", return_value=list(tabs)))
        st.enter_context(patch.object(google_forms, "probe_form", return_value=google_forms.ProbeResult(
            google_forms.OPEN if form_open else google_forms.SIGNIN_REQUIRED, "test")))
        st.enter_context(patch.object(runner.time, "sleep"))
        res = runner.run_module4(s, budget_check=lambda: budget_guard.decide(None, 1.0))
    return res, spawned


def test_runner():
    print("\n[2] runner: park, do not charge, do not drop")
    s = _session()
    j = _job(s, 9001)
    out = agent_driver.ApplyOutcome("stuck", "Google Form requires Google sign-in: " + REAL_FORMS[1379])
    res, spawned = _run(s, outcome=out, tabs=[SIGNIN_TAB])
    s.refresh(j)
    check("agent ran once", spawned == [9001])
    check("no attempt charged", (j.module4_attempts or 0) == 0, str(j.module4_attempts))
    check("still queued (should_apply=1)", j.should_apply is True)
    check("parked with the form URL", google_forms.parked_form_url(j.module4_last_status) == REAL_FORMS[1379],
          j.module4_last_status)
    check("counted as parked", res.google_signin_parked == 1 and res.failed == 0 and res.dropped == 0)

    print("\n[3] runner: a parked job is skipped without an agent while signed out")
    res, spawned = _run(s, outcome=out)
    s.refresh(j)
    check("no agent spawned", spawned == [])
    check("still parked and queued", google_forms.is_parked(j.module4_last_status) and j.should_apply is True)
    check("still no attempt", (j.module4_attempts or 0) == 0)

    print("\n[4] runner: released once the browser is signed in")
    ok = agent_driver.ApplyOutcome("submitted", "Google Form submitted")
    res, spawned = _run(s, outcome=ok, signed_in=True)
    s.refresh(j)
    check("agent spawned", spawned == [9001])
    check("submitted", j.applied is True and res.submitted == 1)

    print("\n[5] runner: released when the form starts opening signed out")
    s2 = _session()
    j2 = _job(s2, 9002, last=google_forms.status_for(REAL_FORMS[841], "x"))
    res, spawned = _run(s2, outcome=ok, form_open=True)
    check("agent spawned", spawned == [9002])

    print("\n[6] runner: signed in and still refused -> an ordinary charged failure (no loop)")
    s3 = _session()
    j3 = _job(s3, 9003)
    res, spawned = _run(s3, outcome=out, signed_in=True, tabs=[SIGNIN_TAB])
    s3.refresh(j3)
    check("attempt charged", j3.module4_attempts == 1, str(j3.module4_attempts))
    check("not parked", not google_forms.is_parked(j3.module4_last_status))
    check("not dropped on the first try", j3.should_apply is True)

    print("\n[7] runner: an old row with a stored wall DETAIL is parked, not dropped")
    s4 = _session()
    j4 = _job(s4, 9004, attempts=1, last="stuck:" + REAL_DETAILS[1379])
    res, spawned = _run(s4, outcome=out)
    s4.refresh(j4)
    check("no agent spawned", spawned == [])
    check("kept in the queue", j4.should_apply is True)
    check("re-labelled as parked", google_forms.is_parked(j4.module4_last_status), j4.module4_last_status)


def test_live():
    print("\n[live] the real forms, signed out")
    for jid, url in REAL_FORMS.items():
        pr = google_forms.probe_form(url)
        check(f"job {jid}: {pr.state} ({pr.reason})", pr.state == google_forms.SIGNIN_REQUIRED)
    cdp = os.environ.get("JOBO_GFORMS_TEST_CDP", "")
    if not cdp:
        print("  (JOBO_GFORMS_TEST_CDP unset: browser checks skipped)")
        return
    assert ":9333" not in cdp, "never the live pipeline browser"
    check("scratch browser is signed out", google_forms.browser_signed_in(cdp) is False)
    tabs = google_forms.page_urls(cdp)
    check("a tab sits on Google sign-in for a form", google_forms.is_signin_wall("", tabs), str(tabs)[:200])


def main() -> int:
    test_detection()
    test_runner()
    if "--live" in sys.argv:
        test_live()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
