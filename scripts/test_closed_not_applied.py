#!/usr/bin/env python
"""Dead listings must be terminal WITHOUT being recorded as applications.

The bug (found 2026-09-15): module4/runner.py called persist.mark_applied()
for `closed` and `already_applied` outcomes. mark_applied hardcodes
applied=True, module4_last_status='submitted' and stamps module4_submitted_at,
so a posting that said "no longer accepting applications" became
indistinguishable from a real submission. 67 historical rows were affected and
every reported total was inflated.

These tests run against a temporary SQLite DB. They never touch data/jobs.db,
never spawn a browser, and never make a model call.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module3 import persist as m3persist  # noqa: E402
from module4 import persist as m4persist  # noqa: E402
from module4 import query as m4query  # noqa: E402

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


def make_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), tmp.name


def new_job(session, jid: int, company: str = "TestCo") -> JobModel:
    job = JobModel(
        id=jid,
        company_name=company,
        job_title="Software Engineer",
        job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False,
        referred_json="[]",
        metadata_json="{}",
        module2_attempted=True,
        module2_enriched_at=datetime.now(),
        should_apply=True,
        module3_fit_score=4,
        module3_resume_pdf_path=f"/tmp/resume_{jid}.pdf",
        module3_cover_pdf_path=f"/tmp/cover_{jid}.pdf",
        module4_attempts=0,
    )
    session.add(job)
    session.commit()
    return job


def test_closed_is_not_applied():
    print("\n[1] a dead listing is terminal but NOT an application")
    session, path = make_session()
    try:
        job = new_job(session, 1, "DeadCo")
        m4persist.mark_closed(
            session, job, status="closed", detail='page says "no longer accepting"'
        )
        session.refresh(job)

        check("applied stays False", job.applied is False, f"got {job.applied!r}")
        check(
            "module4_submitted_at stays empty",
            job.module4_submitted_at is None,
            f"got {job.module4_submitted_at!r}",
        )
        check(
            "status is not 'submitted'",
            not str(job.module4_last_status).startswith("submitted"),
            f"got {job.module4_last_status!r}",
        )
        check(
            "status records closed + reason",
            str(job.module4_last_status).startswith("closed:"),
            f"got {job.module4_last_status!r}",
        )
        check("module4_closed_at is stamped", job.module4_closed_at is not None)
        check("should_apply is cleared", job.should_apply is False)
    finally:
        session.close()
        os.unlink(path)


def test_closed_leaves_the_queue():
    print("\n[2] a dead listing never comes back into the apply queue")
    session, path = make_session()
    try:
        live = new_job(session, 10, "LiveCo")
        dead = new_job(session, 11, "DeadCo")
        m4persist.mark_closed(session, dead, status="closed", detail="dead")

        queued = [j.id for j in m4query.list_jobs_to_apply(session)]
        check("live job is queued", 10 in queued, f"queue={queued}")
        check("dead job is NOT queued", 11 not in queued, f"queue={queued}")

        # triage --force re-scores everything; it must not resurrect the dead one.
        m3persist.apply_phase1_fit(
            session,
            dead,
            fit_score=5,
            reasoning="great fit",
            highlighted_projects_json="[]",
            should_apply=True,
            dry_run=False,
        )
        session.refresh(dead)
        check(
            "re-triage does not set should_apply on a dead listing",
            dead.should_apply is False,
            f"got {dead.should_apply!r}",
        )
        queued_after = [j.id for j in m4query.list_jobs_to_apply(session)]
        check(
            "dead job still absent after re-triage",
            11 not in queued_after,
            f"queue={queued_after}",
        )
        check("live job unaffected", 10 in queued_after, f"queue={queued_after}")
    finally:
        session.close()
        os.unlink(path)


def test_real_submission_still_works():
    print("\n[3] a real submission is still recorded as applied")
    session, path = make_session()
    try:
        job = new_job(session, 20, "RealCo")
        m4persist.mark_applied(session, job)
        session.refresh(job)

        check("applied is True", job.applied is True)
        check("status is submitted", job.module4_last_status == "submitted")
        check("submitted_at is stamped", job.module4_submitted_at is not None)
        check("closed_at stays empty", job.module4_closed_at is None)
        queued = [j.id for j in m4query.list_jobs_to_apply(session)]
        check("applied job leaves the queue", 20 not in queued, f"queue={queued}")
    finally:
        session.close()
        os.unlink(path)


def test_counts_are_separable():
    print("\n[4] applied and dead are countable apart (the reporting bug)")
    session, path = make_session()
    try:
        for jid in (30, 31, 32):
            m4persist.mark_applied(session, new_job(session, jid))
        for jid in (40, 41):
            m4persist.mark_closed(session, new_job(session, jid), status="closed")

        applied = session.query(JobModel).filter(JobModel.applied.is_(True)).count()
        dead = (
            session.query(JobModel)
            .filter(JobModel.module4_closed_at.isnot(None))
            .count()
        )
        check("3 real applications", applied == 3, f"got {applied}")
        check("2 dead listings", dead == 2, f"got {dead}")
        overlap = (
            session.query(JobModel)
            .filter(JobModel.applied.is_(True))
            .filter(JobModel.module4_closed_at.isnot(None))
            .count()
        )
        check("no row is both applied and dead", overlap == 0, f"got {overlap}")
    finally:
        session.close()
        os.unlink(path)


def test_already_applied_outcome():
    print("\n[5] 'already applied per the platform' also sends nothing")
    session, path = make_session()
    try:
        job = new_job(session, 50, "SeenCo")
        m4persist.mark_closed(
            session, job, status="already_applied", detail="platform says applied"
        )
        session.refresh(job)
        check("applied stays False", job.applied is False, f"got {job.applied!r}")
        check(
            "status records already_applied",
            str(job.module4_last_status).startswith("already_applied"),
            f"got {job.module4_last_status!r}",
        )
        check("leaves the queue", job.should_apply is False)
    finally:
        session.close()
        os.unlink(path)


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls
    main(); running the file directly uses the same path."""
    print("dead-listing accounting regressions")
    test_closed_is_not_applied()
    test_closed_leaves_the_queue()
    test_real_submission_still_works()
    test_counts_are_separable()
    test_already_applied_outcome()
    # Report in the runner's convention ("N/N passed"); scripts/
    # run_isolated_regressions.py::check_count parses this and deliberately
    # REJECTS a suite that reports zero checks, so the format matters.
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
