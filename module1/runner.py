import json
import logging
import os
from datetime import datetime
from typing import Optional

from module1.collector import fetch_emails
from module1.parser import parse_email_to_jobs
from module1.store import upsert_job
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

# Temporary: Path to save email samples for analysis
EMAIL_SAMPLES_FILE = "data/email_samples.json"


def _save_email_samples(emails) -> None:
    """Save email samples to JSON file for analysis."""
    samples = []
    for email in emails:
        sample = {
            "message_id": email.message_id,
            "sender": email.sender,
            "subject": email.subject,
            "date": email.date,
            "body_text": email.body_text[:2000] if email.body_text else None,
            "body_html": email.body_html[:5000] if email.body_html else None,
            "links": email.links[:20] if email.links else [],
        }
        samples.append(sample)

    # Load existing samples if file exists
    existing_samples = []
    if os.path.exists(EMAIL_SAMPLES_FILE):
        try:
            with open(EMAIL_SAMPLES_FILE, "r") as f:
                existing_samples = json.load(f)
        except:
            pass

    # Merge and deduplicate by message_id
    all_samples = {s["message_id"]: s for s in existing_samples}
    for sample in samples:
        all_samples[sample["message_id"]] = sample

    # Save back to file
    with open(EMAIL_SAMPLES_FILE, "w") as f:
        json.dump(list(all_samples.values()), f, indent=2)

    logger.info(f"Saved {len(samples)} email samples to {EMAIL_SAMPLES_FILE}")


class CollectorResult:
    def __init__(
        self,
        emails_processed: int = 0,
        jobs_extracted: int = 0,
        new_jobs: int = 0,
        updated_jobs: int = 0,
        errors: Optional[list[str]] = None,
    ):
        self.emails_processed = emails_processed
        self.jobs_extracted = jobs_extracted
        self.new_jobs = new_jobs
        self.updated_jobs = updated_jobs
        self.errors = errors or []


def run(session: Session, hours: int = 24, dry_run: bool = False) -> CollectorResult:
    """
    Orchestrates the full module1 flow: collect → parse → store.

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

        # TEMPORARY: Save email samples to JSON for analysis
        _save_email_samples(emails)
    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        result.errors.append(f"Fetch error: {e}")
        return result

    if dry_run:
        for email in emails:
            logger.info(f"  - {email.subject} from {email.sender}")
        return result

    for email in emails:
        try:
            jobs = parse_email_to_jobs(email)
            result.jobs_extracted += len(jobs)

            for job in jobs:
                _, created = upsert_job(session, job)
                if created:
                    result.new_jobs += 1
                else:
                    result.updated_jobs += 1

        except Exception as e:
            logger.error(f"Failed to parse email {email.message_id}: {e}")
            result.errors.append(f"Parse error ({email.message_id}): {e}")

    logger.info(
        f"Collection complete: {result.jobs_extracted} jobs, {result.new_jobs} new, {result.updated_jobs} updated"
    )
    return result
