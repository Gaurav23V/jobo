"""Persist Module 3 phase 1 / phase 2 to SQLite."""

from sqlalchemy.orm import Session

from db.models import JobModel
from module4.constants import max_attempts_per_job


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
    # A job module4 has already permanently dropped (exhausted its retry
    # budget - see module4/constants.py MAX_ATTEMPTS_PER_JOB) must stay
    # dropped even under `triage --force`. Without this guard, re-triaging a
    # dropped job silently resurrects it into module4's apply queue, undoing
    # the drop decision without anyone asking for that (found 2026-09-02
    # while testing triage --force on already-dropped jobs).
    already_dropped_by_apply = (job.module4_attempts or 0) >= max_attempts_per_job() and not job.applied
    # A dead listing is terminal for the same reason. Before 2026-09-15 these
    # rows carried applied=1, so the queue filter hid them; now they correctly
    # carry applied=0, and only module4_closed_at keeps `triage --force` from
    # pushing a closed posting back into the apply queue.
    listing_is_dead = job.module4_closed_at is not None
    job.should_apply = should_apply and not already_dropped_by_apply and not listing_is_dead
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
