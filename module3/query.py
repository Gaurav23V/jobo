"""Rows eligible for Module 3 triage."""

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from db.models import JobModel


def _path_incomplete(col):
    return or_(col.is_(None), col == "")


def list_jobs_for_module3(session: Session, *, force: bool = False) -> list[JobModel]:
    """Enriched jobs that still need phase 1 and/or phase 2 (unless force=all enriched)."""
    q = session.query(JobModel).filter(JobModel.module2_enriched_at.isnot(None))
    if not force:
        resume_bad = _path_incomplete(JobModel.module3_resume_pdf_path)
        cover_bad = _path_incomplete(JobModel.module3_cover_pdf_path)
        paths_incomplete = or_(resume_bad, cover_bad)
        needs_phase2 = and_(JobModel.should_apply.is_(True), paths_incomplete)
        needs_work = or_(JobModel.module3_fit_score.is_(None), needs_phase2)
        q = q.filter(needs_work)
    return q.order_by(JobModel.id).all()
