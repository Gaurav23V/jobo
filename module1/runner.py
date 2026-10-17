import logging
from typing import Optional

from module1.collector import fetch_emails
from module1.parser import parse_email_to_jobs
from sources.base import JobCandidate
from sources.store import insert_candidates
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


class CollectorResult:
    def __init__(
        self,
        emails_processed: int = 0,
        jobs_extracted: int = 0,
        new_jobs: int = 0,
        updated_jobs: int = 0,
        duplicate_jobs: int = 0,
        errors: Optional[list[str]] = None,
    ):
        self.emails_processed = emails_processed
        self.jobs_extracted = jobs_extracted
        self.new_jobs = new_jobs
        self.updated_jobs = updated_jobs
        self.duplicate_jobs = duplicate_jobs
        self.errors = errors or []


def run(session: Session, hours: int = 24, dry_run: bool = False) -> CollectorResult:
    """Orchestrates the full module1 flow: collect → parse → store.

    Args:
        session: SQLAlchemy session for database operations (provided by main.py)
        hours: Number of hours to look back for emails
        dry_run: If True, parse without writing to DB
    """
    result = CollectorResult()

    logger.info(f"Starting collection for past {hours} hours")

    try:
        emails = fetch_emails(hours)
        result.emails_processed = len(emails)
        logger.info(f"Fetched {len(emails)} emails")
    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        result.errors.append(f"Fetch error: {e}")
        return result

    if dry_run:
        for email in emails:
            logger.info(f"  - {email.subject} from {email.sender}")
        return result

    candidates: list[JobCandidate] = []
    for email in emails:
        try:
            jobs = parse_email_to_jobs(email)
            result.jobs_extracted += len(jobs)
            candidates.extend(
                JobCandidate(job_url=job.job_url, source=job.source_platform or "linkedin")
                for job in jobs
            )
        except Exception as e:
            logger.error(f"Failed to parse email {email.message_id}: {e}")
            result.errors.append(f"Parse error ({email.message_id}): {e}")

    # The shared insert path: cross-source dedup (url, LinkedIn id,
    # company+title), then insert un-enriched rows for module 2.
    inserted = insert_candidates(session, candidates)
    result.new_jobs = inserted.inserted
    result.duplicate_jobs = inserted.duplicates
    result.errors.extend(f"Insert error: {e}" for e in inserted.errors)

    logger.info(
        f"Collection complete: {result.jobs_extracted} jobs, {result.new_jobs} new, {result.updated_jobs} updated"
    )
    return result
