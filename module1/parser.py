"""Email parser for extracting job listings from LinkedIn job alert emails."""

import logging
import re
from typing import Optional

from db.models import Job
from module1.collector import RawEmail


logger = logging.getLogger(__name__)

# LinkedIn job alert sender
LINKEDIN_JOB_ALERT_SENDER = "jobalerts-noreply@linkedin.com"


def _normalize_linkedin_url(url: str) -> Optional[str]:
    """Extract clean LinkedIn job URL from tracking URL.

    Converts URLs like:
    - linkedin.com/comm/jobs/view/12345?trackingId=...
    - linkedin.com/jobs/view/12345?refId=...

    To clean format:
    - https://www.linkedin.com/jobs/view/12345/

    Returns None if the URL is not a LinkedIn job view link.
    """
    match = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url, re.IGNORECASE)
    if match:
        job_id = match.group(1)
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return None


def parse_email_to_jobs(email: RawEmail) -> list[Job]:
    """Parse LinkedIn job alert email into Job records.

    Only processes emails from LinkedIn job alerts. Job URLs come from
    ``email.links`` (href targets extracted from the HTML part in the collector),
    each normalized to ``https://www.linkedin.com/jobs/view/<id>/``. Other Job
    fields use dataclass defaults (e.g. company/title/location unset).

    Args:
        email: RawEmail object from collector

    Returns:
        List of Job objects with ``job_url`` (and ``source_platform``) set.
    """
    jobs = []

    # Only process LinkedIn job alerts
    if LINKEDIN_JOB_ALERT_SENDER not in email.sender:
        logger.debug(f"Skipping non-LinkedIn job alert: {email.sender}")
        return jobs

    logger.info(f"Processing LinkedIn job alert: {email.subject[:50]}...")

    processed_urls = set()

    for link in email.links:
        clean_url = _normalize_linkedin_url(link)
        if not clean_url or clean_url in processed_urls:
            continue

        processed_urls.add(clean_url)
        jobs.append(Job(job_url=clean_url, source_platform="linkedin"))

    logger.info(f"Extracted {len(jobs)} jobs from LinkedIn email")
    return jobs
