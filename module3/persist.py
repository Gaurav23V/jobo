"""Persist Module 3 phase 1 / phase 2 to SQLite."""

from sqlalchemy.orm import Session

from db.models import JobModel


def apply_phase1_fit(
    session: Session,
    job: JobModel,
    *,
    fit_score: int,
    reasoning: str,
    highlighted_projects_json: str,
    should_apply: bool,
    dry_run: bool,
) -> None:
    job.module3_fit_score = fit_score
    job.module3_fit_reasoning = reasoning
    job.module3_highlighted_projects = highlighted_projects_json
    job.should_apply = should_apply
    if dry_run:
        return
    session.add(job)
    session.commit()


def apply_phase2_paths(
    session: Session,
    job: JobModel,
    *,
    resume_pdf_path: str,
    cover_pdf_path: str,
    dry_run: bool,
) -> None:
    job.module3_resume_pdf_path = resume_pdf_path
    job.module3_cover_pdf_path = cover_pdf_path
    if dry_run:
        return
    session.add(job)
    session.commit()
