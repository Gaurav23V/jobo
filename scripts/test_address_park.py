#!/usr/bin/env python
"""Forms that require a street address / postal code are parked under one marker (GHO-109).

The 2026-10-30 22:00 run had 6 of 13 stuck jobs stop on a required street
address or PIN/ZIP code that the candidate facts lack. The same stop was
filed as a non-retryable drop or as a retryable stuck depending only on the
agent's wording, and the run report filed each under other:stuck at its own
host. Covers:
  * module4/address_wall.py + blockers.address_park: every wording parks; a
    photo / DOB demand keeps its own verdict; nearby non-walls do not park
  * runner: a fresh stop and a stored stop are parked, no attempt charged;
    a parked job is skipped without an agent and stays out of the queue
  * run_report: one signature, personal:address (needs_g)
  * scripts/requeue_address_parked.py: --backfill parks legacy rows, the
    default mode releases parked ones with attempts reset

Offline: temporary SQLite DB, no browser, no model calls, no subprocesses.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import address_wall, agent_driver, blockers, budget_guard, plan, preflight, query, runner  # noqa: E402
from module4.run_report import signature_for  # noqa: E402

FAKE_PDF = Path(tempfile.gettempdir()) / "jobo_test_address_resume.pdf"

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


# The six DETAIL lines of the 2026-10-30 22:00 run and earlier (data/jobs.db
# module4_last_status), employer names pseudonymised. Three were dropped for
# good, two left retryable, one dropped after 3/3: one wall.
ADDRESS_WALLS = [
    "Northwind AI's Greenhouse form requires a Zip Code/Postal Code field to submit, which is not in the "
    "candidate facts and the hard rules forbid inventing",
    "LinkedIn Easy Apply for Tailspin Tools requires Address line 1 and Zip/Postal code (not in candidate facts) "
    "to advance past the Contact Info step; blocked without inventing data",
    "Fabrikam Workday application (My Information step) requires Address Line 1 and Postal Code, neither of which "
    "is in the candidate facts; form will not proceed without them",
    "Litware Logistics' Tenstreet application (Personal Information step) requires a street address and ZIP/postal "
    "code to proceed past step 2 of 10, and neither value is in the candidate facts",
    "Oracle Cloud Recruiting form for Contoso requires Address Line 1 (street address) and Pin Code, neither of "
    "which is in the candidate facts; all other fields completed and resume verified, but Submit is blocked on "
    "these two required fields.",
    "adatum.paradox.ai application form requires Street Address and Pin Code (postal code) on the Personal "
    "Information step, which are not in the candidate facts",
]
# Walls a street address would not clear: they keep their own verdict.
PHOTO_TOO = ("Woodgrove careers form requires Address line 1, Zip/Postal code, Gender, and a Candidate Photo upload "
             "to proceed past the registration/profile step, none of which are in the candidate facts, and the "
             "form will not advance without them")
DOB_TOO = ("Form requires Date of Birth and Address Line 1, neither of which is in the candidate facts")
NOT_ADDRESS_WALLS = [
    # Fields named, but nothing missing from the facts.
    "Address Line 1 dropdown would not keep its value after three tries; required field left empty",
    "Postal code typed from the candidate facts but the Workday Next button won't advance",
    # Missing from the facts, but not an address.
    "Myrtleby's form requires mandatory 10th/12th percentage fields not present in candidate facts",
    "Form requires a valid email address and phone; the confirmation email never arrived",
    "",
]


def test_classifier():
    print("\n[1] one verdict for every address wording")
    for d in ADDRESS_WALLS:
        check(f"parks: {d[:60]}...", blockers.address_park(d))
        check(f"not dropped: {d[:60]}...", blockers.permanent_reason("stuck", d) is None,
              repr(blockers.permanent_reason("stuck", d)))
    check("photo + address: photo-skip, not parked",
          not blockers.address_park(PHOTO_TOO) and blockers.photo_required(PHOTO_TOO)
          and address_wall.is_address_wall(PHOTO_TOO))
    check("DOB + address: dropped, not parked",
          not blockers.address_park(DOB_TOO) and blockers.permanent_reason("stuck", DOB_TOO) is not None)
    for d in NOT_ADDRESS_WALLS:
        check(f"not an address wall: {d[:55] or '<empty>'}...", not blockers.address_park(d))
    st = address_wall.status_for(ADDRESS_WALLS[2])
    check("parked status carries the marker and the detail",
          address_wall.is_parked(st) and st.startswith("needs_address: ") and "Postal Code" in st)
    check("a stuck status is not parked", not address_wall.is_parked(f"stuck:{ADDRESS_WALLS[2]}"))


def test_signature():
    print("\n[2] run report: one signature, needs_g")
    for d in ADDRESS_WALLS:
        sig = signature_for(f"stuck:{d}", "Contoso")
        check(f"stuck row -> personal:address: {d[:45]}...", sig == "personal:address", sig)
        sig = signature_for(address_wall.status_for(d), "Contoso")
        check(f"parked row -> personal:address: {d[:45]}...", sig == "personal:address", sig)
    from module4.run_report import _RULE_BUCKET
    check("personal:address is needs_g", _RULE_BUCKET.get("personal:address") == "needs_g")
    check("email-address wording is not personal:address",
          signature_for(f"stuck:{NOT_ADDRESS_WALLS[3]}") != "personal:address")


def make_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), tmp.name


def add_job(session, jid, *, last_status=None, attempts=0, should_apply=True):
    session.add(JobModel(
        id=jid, company_name=f"Co{jid}", job_title="SWE",
        job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False, referred_json="[]", metadata_json="{}",
        module2_attempted=True, module2_enriched_at=datetime.now(),
        should_apply=should_apply, module3_fit_score=4,
        module3_resume_pdf_path=str(FAKE_PDF), module3_cover_pdf_path=str(FAKE_PDF),
        module4_attempts=attempts, module4_last_status=last_status,
    ))
    session.commit()


def run_with(session, outcomes: dict, **kw):
    """run_module4 with every external dependency faked. outcomes: job_id -> ApplyOutcome."""
    spawned = []

    def fake_agent(*, job_id, **_):
        spawned.append(job_id)
        return outcomes[job_id]

    open_pf = preflight.PreflightResult(closed=False, reason="open", checked=True)
    with ExitStack() as st:
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
        result = runner.run_module4(session, budget_check=lambda: budget_guard.decide(None, 1.0), **kw)
    return result, spawned


def test_runner():
    print("\n[3] runner: parked, never charged, never retried")
    FAKE_PDF.write_bytes(b"%PDF-fake")
    Outcome = agent_driver.ApplyOutcome
    session, db = make_session()
    try:
        add_job(session, 1)  # fresh: the agent meets the wall now
        add_job(session, 2, attempts=1, last_status=f"stuck:{ADDRESS_WALLS[2]}")  # left retryable before
        add_job(session, 3, attempts=0, last_status=address_wall.status_for(ADDRESS_WALLS[0]))  # already parked
        add_job(session, 4)  # photo + address: still the GHO-60 photo skip
        outcomes = {
            1: Outcome(status="stuck", detail=ADDRESS_WALLS[3]),
            4: Outcome(status="stuck", detail=PHOTO_TOO),
        }
        result, spawned = run_with(session, outcomes)
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("fresh address stop -> parked, no attempt charged",
              address_wall.is_parked(rows[1].module4_last_status) and rows[1].module4_attempts == 0
              and rows[1].should_apply is True and rows[1].module4_outcome == "stuck",
              f"{rows[1].module4_last_status[:60]!r} attempts={rows[1].module4_attempts}")
        check("stored retryable address stop -> parked without an agent",
              2 not in spawned and address_wall.is_parked(rows[2].module4_last_status)
              and rows[2].should_apply is True and rows[2].module4_attempts == 1,
              f"spawned={spawned} {rows[2].module4_last_status[:60]!r}")
        check("parked job is not in the queue", 3 not in spawned and 3 not in {j.id for j in query.list_jobs_to_apply(session)})
        check("photo + address -> photo policy skip",
              rows[4].module4_outcome == "skipped_by_policy" and rows[4].should_apply is False)
        check("counter: two parked this run", result.address_parked == 2, str(result.address_parked))
        check("plan explains the parked job",
              "street address" in plan._filtered_reason(rows[3]), plan._filtered_reason(rows[3]))

        # --job-id still hands the parked job to the runner; it is skipped there.
        result, spawned = run_with(session, {}, job_id=3)
        check("--job-id on a parked job: no agent, still parked",
              spawned == [] and result.address_parked == 1
              and address_wall.is_parked(session.get(JobModel, 3).module4_last_status))
        result, spawned = run_with(session, {3: Outcome(status="submitted", detail="confirmation page")},
                                   job_id=3, force=True)
        check("--force reaches a parked job", spawned == [3])
    finally:
        session.close()
        os.unlink(db)


def test_requeue_script():
    print("\n[4] scripts/requeue_address_parked.py: backfill, then release")
    sys.path.insert(0, str(REPO / "scripts"))
    import requeue_address_parked as rq

    session, db = make_session()
    try:
        add_job(session, 10, attempts=1, should_apply=False, last_status=f"stuck:{ADDRESS_WALLS[0]}")  # dropped
        add_job(session, 11, attempts=1, last_status=f"stuck:{ADDRESS_WALLS[2]}")  # left retryable
        add_job(session, 12, attempts=3, should_apply=False, last_status=f"stuck:{ADDRESS_WALLS[4]}")  # 3/3
        add_job(session, 13, attempts=1, should_apply=False,
                last_status=f"skipped: G decision (GHO-60): photo upload required; last attempt: stuck:{PHOTO_TOO}")
        add_job(session, 14, attempts=1, should_apply=False, last_status=f"stuck:{ADDRESS_WALLS[1]}")
        add_job(session, 15, attempts=2, last_status="stuck:ran out of step budget (80 model steps) mid-form")
        j = session.get(JobModel, 14)
        j.module4_closed_at = datetime.now()  # closed posting: never touched
        session.commit()
    finally:
        session.close()

    def run(*argv):
        out = StringIO()
        with redirect_stdout(out):
            rc = rq.main(["--db", db, *argv])
        return rc, out.getvalue()

    def rows():
        con = sqlite3.connect(db)
        try:
            return {r[0]: r[1:] for r in con.execute(
                "select id, should_apply, module4_attempts, module4_last_status, module4_outcome from jobs")}
        finally:
            con.close()

    try:
        before = rows()
        rc, out = run("--backfill")
        check("backfill dry run writes nothing", rc == 0 and rows() == before and "would be parked" in out, out)
        rc, out = run("--backfill", "--apply")
        r = rows()
        check("backfill parks the dropped, retryable and 3/3 rows",
              all(r[i][0] == 1 and address_wall.is_parked(r[i][2]) for i in (10, 11, 12)), str(r))
        check("backfill keeps attempts", (r[10][1], r[11][1], r[12][1]) == (1, 1, 3))
        check("backfill leaves the photo skip, the closed posting and other failures alone",
              r[13] == before[13] and r[14] == before[14] and r[15] == before[15])
        rc, out = run("--backfill")
        check("backfill is idempotent", "nothing to be parked" in out, out)

        rc, out = run()
        check("release dry run lists the three parked jobs and writes nothing",
              out.count(": release (") == 3 and rows() == r, out)
        rc, out = run("--apply")
        r2 = rows()
        check("release: queued again as a fresh job",
              all(r2[i][:3] == (1, 0, None) and r2[i][3] == "not_attempted" for i in (10, 11, 12)), str(r2))
        check("release leaves everything else alone", all(r2[i] == r[i] for i in (13, 14, 15)))
    finally:
        os.unlink(db)


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls main()."""
    print("address / postal-code walls are parked (GHO-109)")
    test_classifier()
    test_signature()
    test_runner()
    test_requeue_script()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
