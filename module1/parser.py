"""Email parser for extracting job listings from LinkedIn job alert emails."""

import logging
import re
from datetime import datetime
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
    """
    match = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url, re.IGNORECASE)
    if match:
        job_id = match.group(1)
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return None


def _is_linkedin_job_url(url: str) -> bool:
    """Check if URL is a LinkedIn job posting URL."""
    return bool(re.search(r"linkedin\.com/(?:comm/)?jobs/view/\d+", url, re.IGNORECASE))


def _extract_jobs_from_text(body_text: Optional[str]) -> dict:
    """Extract job details from LinkedIn email text body.

    Parses text blocks separated by dashes and extracts:
    - Job title
    - Company name
    - Location
    - Job URL

    Returns dict mapping clean URL to job details.
    """
    jobs = {}

    if not body_text:
        return jobs

    # Split by separator line (20+ dashes)
    job_blocks = re.split(r"-{20,}", body_text)

    for block in job_blocks:
        # Find the "View job:" URL
        url_match = re.search(r"View job:\s*(https?://\S+)", block, re.IGNORECASE)
        if not url_match:
            continue

        raw_url = url_match.group(1).strip()
        clean_url = _normalize_linkedin_url(raw_url)
        if not clean_url:
            continue

        # Parse lines, filtering out status messages
        lines = [line.strip() for line in block.split("\n") if line.strip()]

        # Find index of "View job:" line
        view_job_idx = -1
        for i, line in enumerate(lines):
            if line.startswith("View job:") or line.startswith("View job :"):
                view_job_idx = i
                break

        if view_job_idx == -1:
            continue

        # Filter out non-content lines
        skip_patterns = [
            "actively hiring",
            "apply with",
            "new jobs match",
            "your job alert",
            "view job",
            "connection",
            "easy apply",
            "this company is",
            "resume & profile",
        ]

        candidates = []
        for i in range(max(0, view_job_idx - 10), view_job_idx):
            line = lines[i]
            if not any(pattern in line.lower() for pattern in skip_patterns):
                if len(line) > 2 and not line.startswith("http"):
                    candidates.append(line)

        # Extract title, company, location from last 3 candidates
        title = None
        company = None
        location = None

        if len(candidates) >= 3:
            title = candidates[-3]
            company = candidates[-2]
            location = candidates[-1]
        elif len(candidates) == 2:
            title = candidates[-2]
            company = candidates[-1]
        elif len(candidates) == 1:
            title = candidates[-1]

        jobs[clean_url] = {"title": title, "company": company, "location": location}

    return jobs


def parse_email_to_jobs(email: RawEmail) -> list[Job]:
    """Parse LinkedIn job alert email into Job records.

    Only processes emails from LinkedIn job alerts. Extracts all job links,
    normalizes URLs, and pulls job details from text body when available.

    Args:
        email: RawEmail object from collector

    Returns:
        List of Job objects (may have null company/title if details not in email)
    """
    jobs = []

    # Only process LinkedIn job alerts
    if LINKEDIN_JOB_ALERT_SENDER not in email.sender:
        logger.debug(f"Skipping non-LinkedIn job alert: {email.sender}")
        return jobs

    logger.info(f"Processing LinkedIn job alert: {email.subject[:50]}...")

    # Extract job details from text body
    text_jobs = _extract_jobs_from_text(email.body_text)

    # Process all job links in email
    processed_urls = set()

    for link in email.links:
        if not _is_linkedin_job_url(link):
            continue

        clean_url = _normalize_linkedin_url(link)
        if not clean_url or clean_url in processed_urls:
            continue

        processed_urls.add(clean_url)

        # Get job details if available in text
        details = text_jobs.get(clean_url, {})

        job = Job(
            job_url=clean_url,
            company_name=details.get("company"),
            job_title=details.get("title"),
            location=details.get("location"),
            source_platform="linkedin",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        jobs.append(job)

    logger.info(f"Extracted {len(jobs)} jobs from LinkedIn email")
    return jobs
