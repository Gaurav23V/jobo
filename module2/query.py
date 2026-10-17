import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import JobModel

logger = logging.getLogger(__name__)

# Rows a source inserted already enriched (sources/base.py) carry this as
# module2_model. Module 2 must never scrape them: their job_url is the apply
# link (often an employer ATS), not a LinkedIn listing to read, and a forced
# re-enrich would overwrite the source's metadata with a scrape of that page.
PRE_ENRICHED_SOURCES = ("jobfound",)


def is_pre_enriched(job: JobModel) -> bool:
    return job.module2_model in PRE_ENRICHED_SOURCES or job.source_platform in PRE_ENRICHED_SOURCES


def list_jobs_for_enrichment(
    session: Session, *, force: bool = False, job_id: int | None = None
) -> list[JobModel]:
    """Rows to process: all with module2_attempted false, unless force is True.

    job_id restricts to one row (a failed scrape stays attempted forever, so
    re-enriching it needs `--job-id N --force`).

    Pre-enriched rows (PRE_ENRICHED_SOURCES) are never returned, not even under
    --force or --job-id; asking for one is logged as skipped.
    """
    q = session.query(JobModel)
    if job_id is not None:
        q = q.filter(JobModel.id == job_id)
        row = q.one_or_none()
        if row is not None and is_pre_enriched(row):
            logger.warning(
                "job_id=%s skipped: enriched by its source (%s), module 2 never re-enriches it",
                row.id, row.module2_model or row.source_platform,
            )
            return []
    if not force:
        q = q.filter(JobModel.module2_attempted.is_(False))
    elif job_id is None:
        skipped = sum(1 for r in q.with_entities(JobModel.module2_model, JobModel.source_platform)
                      if r[0] in PRE_ENRICHED_SOURCES or r[1] in PRE_ENRICHED_SOURCES)
        if skipped:
            logger.info("--force: %d row(s) enriched by their source skipped (never re-enriched)", skipped)
    # coalesce: a LinkedIn row's module2_model is NULL until it is enriched,
    # and NOT IN on a NULL would drop it.
    q = q.filter(func.coalesce(JobModel.module2_model, "").notin_(PRE_ENRICHED_SOURCES)).filter(
        func.coalesce(JobModel.source_platform, "").notin_(PRE_ENRICHED_SOURCES)
    )
    return q.order_by(JobModel.id).all()
