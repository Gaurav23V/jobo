from sqlalchemy.orm import Session

from db.models import JobModel


def list_jobs_for_enrichment(
    session: Session, *, force: bool = False, job_id: int | None = None
) -> list[JobModel]:
    """Rows to process: all with module2_attempted false, unless force is True.

    job_id restricts to one row (a failed scrape stays attempted forever, so
    re-enriching it needs `--job-id N --force`).
    """
    q = session.query(JobModel)
    if job_id is not None:
        q = q.filter(JobModel.id == job_id)
    if not force:
        q = q.filter(JobModel.module2_attempted.is_(False))
    return q.order_by(JobModel.id).all()
