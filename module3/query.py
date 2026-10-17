"""Rows eligible for Module 3 triage."""

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from db.models import JobModel
from policy import employer_blocklist


def _path_incomplete(col):
    return or_(col.is_(None), col == "")


def list_jobs_for_module3(
    session: Session,
    *,
    force: bool = False,
    job_id: int | None = None,
    queued: bool = False,
) -> list[JobModel]:
    """Enriched jobs that still need phase 1 and/or phase 2 (unless force=all enriched).

    job_id: restrict to a single job (still subject to the same idempotency
    filter below unless force=True) — used for one-off testing without
    running the full triage pass.

    Blocked employers are dropped here so we never even spend an LLM fit call
    or generate PDFs for a company we will never apply to. `force` does not
    override the blocklist by design.
    """
    q = session.query(JobModel).filter(JobModel.module2_enriched_at.isnot(None))
    if job_id is not None:
        q = q.filter(JobModel.id == job_id)
    if queued:
        # Only the live apply queue (same filter module4 uses): should_apply,
        # not applied, not a dead listing. The runner re-runs both phases for
        # these, which is how materials are refreshed after a base-resume or
        # profile change without re-scoring every enriched row.
        q = (
            q.filter(JobModel.should_apply.is_(True))
            .filter(JobModel.applied.isnot(True))
            .filter(JobModel.module4_closed_at.is_(None))
        )
    elif not force:
        resume_bad = _path_incomplete(JobModel.module3_resume_pdf_path)
        cover_bad = _path_incomplete(JobModel.module3_cover_pdf_path)
        paths_incomplete = or_(resume_bad, cover_bad)
        needs_phase2 = and_(JobModel.should_apply.is_(True), paths_incomplete)
        needs_work = or_(JobModel.module3_fit_score.is_(None), needs_phase2)
        q = q.filter(needs_work)
    rows = q.order_by(JobModel.id).all()
    allowed, blocked = employer_blocklist.partition(rows)
    if blocked:
        employer_blocklist.log_blocked(blocked, "triage")
    return allowed
