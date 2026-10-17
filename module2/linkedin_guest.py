"""Fallback for LinkedIn postings the browser scrape could not read.

Enrich opens each /jobs/view/<id>/ page in Playwright. Some pages never render
the company, location and description in time (three tries, 15 s of waits):
1687, 1727 and 1738 on 2026-10-14 ended as `incomplete_extraction` and never
reached triage. LinkedIn's public guest endpoint returns the same posting as a
small static HTML fragment with all four fields, and it was complete for all
three. It is only used when the browser result is still incomplete.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
_JOB_ID_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


@dataclass
class GuestPosting:
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    closed: bool = False


def linkedin_job_id(job_url: str) -> Optional[str]:
    m = _JOB_ID_RE.search(job_url or "")
    return m.group(1) if m else None


def _text(soup: BeautifulSoup, *selectors: str, sep: str = " ") -> Optional[str]:
    for sel in selectors:
        node = soup.select_one(sel)
        if node is not None:
            t = node.get_text(sep, strip=True)
            if t:
                return t
    return None


def parse_guest_posting(html: str) -> GuestPosting:
    """Pull the four fields (and the closed flag) out of a guest fragment."""
    soup = BeautifulSoup(html or "", "html.parser")
    closed = bool(
        soup.select_one(".closed-job__flavor--closed")
        or re.search(r"(no longer|not currently) accepting applications", soup.get_text(" ", strip=True), re.I)
    )
    return GuestPosting(
        title=_text(soup, ".top-card-layout__title", ".topcard__title"),
        company=_text(soup, ".topcard__org-name-link", ".topcard__flavor"),
        location=_text(soup, ".topcard__flavor--bullet"),
        description=_text(soup, ".show-more-less-html__markup", ".description__text", sep="\n"),
        closed=closed,
    )


def fetch_guest_posting(job_url: str, *, timeout: float = 20.0) -> Optional[GuestPosting]:
    """GET the guest fragment. None on any failure (429, 404, network)."""
    jid = linkedin_job_id(job_url)
    if not jid:
        return None
    try:
        r = httpx.get(
            GUEST_URL.format(id=jid),
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        logger.info("LinkedIn guest fetch failed url=%s: %s", job_url, e)
        return None
    if r.status_code != 200:
        logger.info("LinkedIn guest fetch url=%s status=%s", job_url, r.status_code)
        return None
    return parse_guest_posting(r.text)
