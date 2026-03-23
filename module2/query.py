from sqlalchemy.orm import Session

from db.models import JobModel


def list_jobs_for_enrichment(session: Session, *, force: bool = False) -> list[JobModel]:
    """Rows to process: all with module2_attempted false, unless force is True."""
    q = session.query(JobModel)
    if not force:
        q = q.filter(JobModel.module2_attempted.is_(False))
    return q.order_by(JobModel.id).all()
