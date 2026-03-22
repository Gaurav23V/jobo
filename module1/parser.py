import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from db.models import Job
from module1.collector import RawEmail


logger = logging.getLogger(__name__)


def _detect_platform(sender: str) -> str:
    """Detect the job platform based on sender email."""
    sender_lower = sender.lower()

    if "linkedin" in sender_lower:
        return "linkedin"
    elif "indeed" in sender_lower:
        return "indeed"
    elif "glassdoor" in sender_lower:
        return "glassdoor"
    elif "ziprecruiter" in sender_lower:
        return "ziprecruiter"
    elif "monster" in sender_lower:
        return "monster"
    elif "simplyhired" in sender_lower:
        return "simplyhired"
    else:
        return "unknown"


def _is_job_url(url: str, platform: str) -> bool:
    """Check if a URL is likely a job posting URL."""
    url_lower = url.lower()

    if platform == "linkedin":
        return "/jobs/" in url_lower or "/view/" in url_lower
    elif platform == "indeed":
        return "viewjob" in url_lower or "/jobs/" in url_lower
    elif platform == "glassdoor":
        return "/job/" in url_lower or "/jobs/" in url_lower
    else:
        job_patterns = ["/job", "/jobs", "/career", "/position", "/opening"]
        return any(pattern in url_lower for pattern in job_patterns)


def _clean_text(text: str) -> str:
    """Clean and normalize text."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return text.strip()


def _parse_linkedin_email(email: RawEmail) -> list[Job]:
    """Parse LinkedIn job alert emails."""
    jobs = []

    if not email.body_html:
        logger.warning("LinkedIn email has no HTML body")
        return jobs

    soup = BeautifulSoup(email.body_html, "html.parser")
    job_links = soup.find_all("a", href=re.compile(r"linkedin\.com/jobs/"))

    for link in job_links:
        href = link.get("href")
        if not href or not isinstance(href, str):
            continue
        job_url = href.split("?")[0]
        if not job_url:
            continue

        job_title = _clean_text(link.get_text())
        if not job_title:
            continue

        company_name = "Unknown"
        location = ""

        parent = link.parent
        if parent:
            text = parent.get_text()
            company_match = re.search(r"at\s+(.+?)(?:\s+in|\s*$)", text, re.I)
            if company_match:
                company_name = _clean_text(company_match.group(1))

            location_match = re.search(r"in\s+(.+?)(?:\s+\(|\s*$)", text, re.I)
            if location_match:
                location = _clean_text(location_match.group(1))

        job = Job(
            company_name=company_name,
            job_title=job_title,
            job_url=job_url,
            location=location if location else None,
            source_platform="linkedin",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        jobs.append(job)

    return _deduplicate_jobs(jobs)


def _parse_indeed_email(email: RawEmail) -> list[Job]:
    """Parse Indeed job alert emails."""
    jobs = []

    if not email.body_html:
        logger.warning("Indeed email has no HTML body")
        return jobs

    soup = BeautifulSoup(email.body_html, "html.parser")
    job_links = soup.find_all("a", href=re.compile(r"indeed\.com/viewjob"))

    for link in job_links:
        href = link.get("href")
        if not href or not isinstance(href, str):
            continue
        job_url = href.split("?")[0]
        if not job_url:
            continue

        job_title = _clean_text(link.get_text())
        if not job_title:
            continue

        company_name = "Unknown"
        location = ""

        parent = link.parent
        if parent:
            text = parent.get_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            for i, line in enumerate(lines):
                if job_title in line and i + 1 < len(lines):
                    potential = lines[i + 1]
                    if potential and len(potential) < 100:
                        company_name = potential
                        break

            for line in lines:
                if re.search(r"\w+,\s*\w{2}", line):
                    location = line
                    break

        job = Job(
            company_name=company_name,
            job_title=job_title,
            job_url=job_url,
            location=location if location else None,
            source_platform="indeed",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        jobs.append(job)

    return _deduplicate_jobs(jobs)


def _parse_glassdoor_email(email: RawEmail) -> list[Job]:
    """Parse Glassdoor job alert emails."""
    jobs = []

    if not email.body_html:
        logger.warning("Glassdoor email has no HTML body")
        return jobs

    soup = BeautifulSoup(email.body_html, "html.parser")
    job_links = soup.find_all("a", href=re.compile(r"glassdoor\.com/job/"))

    for link in job_links:
        href = link.get("href")
        if not href or not isinstance(href, str):
            continue
        job_url = href.split("?")[0]
        if not job_url:
            continue

        job_title = _clean_text(link.get_text())
        if not job_title:
            continue

        company_name = "Unknown"
        url_match = re.search(r"/job/([^-]+)", job_url)
        if url_match:
            company_name = url_match.group(1).replace("-", " ").title()

        job = Job(
            company_name=company_name,
            job_title=job_title,
            job_url=job_url,
            location=None,
            source_platform="glassdoor",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        jobs.append(job)

    return _deduplicate_jobs(jobs)


def _parse_generic_email(email: RawEmail) -> list[Job]:
    """Generic parser for unknown platforms using heuristics."""
    jobs = []

    if not email.links:
        logger.debug(f"Email {email.message_id} has no links")
        return jobs

    job_urls = [url for url in email.links if _is_job_url(url, "unknown")][:10]

    for job_url in job_urls:
        job_title = "Unknown Position"
        company_name = "Unknown"

        # Try to extract from URL path
        url_parts = job_url.split("/")
        for part in url_parts:
            if any(
                keyword in part.lower()
                for keyword in [
                    "engineer",
                    "developer",
                    "manager",
                    "analyst",
                    "designer",
                    "lead",
                ]
            ):
                cleaned = part.replace("-", " ").replace("_", " ")
                if 5 < len(cleaned) < 100:
                    job_title = cleaned.title()
                    break

        # Try to extract company from subject
        if email.subject:
            patterns = [
                r"at\s+([A-Z][A-Za-z0-9\s]+?)(?:\s+in|\s*$)",
                r"([A-Z][A-Za-z0-9\s]+?)\s+(?:is hiring|jobs)",
            ]
            for pattern in patterns:
                match = re.search(pattern, email.subject, re.I)
                if match:
                    company_name = match.group(1).strip()
                    break

        job = Job(
            company_name=company_name,
            job_title=job_title[:200],
            job_url=job_url,
            location=None,
            source_platform="unknown",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        jobs.append(job)

    return _deduplicate_jobs(jobs)


def _deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Remove duplicate jobs based on URL."""
    seen_urls = set()
    unique_jobs = []
    for job in jobs:
        if job.job_url not in seen_urls:
            seen_urls.add(job.job_url)
            unique_jobs.append(job)
    return unique_jobs


def parse_email_to_jobs(email: RawEmail) -> list[Job]:
    """
    Parse a RawEmail into a list of Job records.

    Uses platform-specific parsing for known job boards (LinkedIn, Indeed, Glassdoor)
    and falls back to generic heuristics for unknown platforms.

    Args:
        email: RawEmail object containing email data

    Returns:
        List of Job objects extracted from the email
    """
    platform = _detect_platform(email.sender)
    logger.info(f"Parsing email from {email.sender} as {platform}")

    if platform == "linkedin":
        jobs = _parse_linkedin_email(email)
    elif platform == "indeed":
        jobs = _parse_indeed_email(email)
    elif platform == "glassdoor":
        jobs = _parse_glassdoor_email(email)
    else:
        jobs = _parse_generic_email(email)

    logger.info(f"Extracted {len(jobs)} jobs from email")
    return jobs
