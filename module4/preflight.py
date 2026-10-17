"""Cheap pre-check for LinkedIn postings that are already closed.

Why this exists
---------------
On the 2026-09-01 apply run, 31 of 63 attempted jobs (49%) came back `closed`
because the LinkedIn posting said "No longer accepting applications". Each one
cost a full apply-agent spawn: ~2 minutes of agent time plus, under the old
pacing rule, a ~112s sleep afterwards. That is roughly two hours of a four-hour
window spent discovering that jobs were dead.

This module answers the same question with one HTTP GET (~1s) before any agent
is spawned.

Deliberately conservative
-------------------------
A false "closed" verdict means silently skipping a job the user could have applied to,
which is far worse than wasting two minutes. So:

  * only LinkedIn job URLs are ever checked (the phrasing below is LinkedIn's;
    external ATS portals word it a hundred different ways)
  * a job is called closed ONLY on an exact known LinkedIn phrase
  * anything ambiguous - network error, redirect, login wall, unexpected
    markup, empty body - returns UNKNOWN, and UNKNOWN always falls through to
    the agent
  * the sanity check requires the page to actually look like a job posting
    before a "closed" verdict is trusted, so a login/error page containing
    stray words can't produce a false positive

In short: this can only ever save time on jobs that are provably dead. It
cannot cause a missed application.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Phrases LinkedIn renders on a closed posting. Matched case-insensitively
# against the page text. Every one of these was observed verbatim in the
# 2026-09-01 apply-run agent reports, except "not currently accepting
# applications", LinkedIn's wording since October 2026 (GHO-24).
CLOSED_PHRASES = (
    "no longer accepting applications",
    "not currently accepting applications",
    "this job is no longer available",
    "no longer available",
    "this job has been closed",
    "applications are no longer being accepted",
)

# The page must look like a real job posting for a closed verdict to count.
# Guards against a login wall / error page / captcha tripping a false positive.
POSTING_MARKERS = (
    "job",
    "linkedin",
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

FETCH_TIMEOUT_SEC = 12
MAX_BYTES = 400_000


@dataclass
class PreflightResult:
    """closed=True only when provably dead; otherwise fall through to the agent."""

    closed: bool
    reason: str = ""
    checked: bool = False  # False = we didn't/couldn't form an opinion

    @property
    def should_skip(self) -> bool:
        return self.closed and self.checked


def _is_linkedin_job_url(url: str) -> bool:
    return bool(re.match(r"^https?://([a-z0-9-]+\.)*linkedin\.com/jobs/view/", url or "", re.I))


def _strip_markup(html: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    return re.sub(r"\s+", " ", text)


def check_job_open(url: str, *, timeout: float = FETCH_TIMEOUT_SEC) -> PreflightResult:
    """One cheap GET to decide whether a LinkedIn posting is provably closed."""
    if not _is_linkedin_job_url(url):
        return PreflightResult(closed=False, reason="not a LinkedIn job URL", checked=False)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        # 404/410 would plausibly mean "gone", but LinkedIn also serves these
        # to bots on live postings. Not worth the false-positive risk.
        return PreflightResult(closed=False, reason=f"http {e.code}", checked=False)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return PreflightResult(closed=False, reason=f"fetch failed: {e}", checked=False)

    if not raw:
        return PreflightResult(closed=False, reason="empty response", checked=False)

    text = _strip_markup(raw.decode("utf-8", errors="replace")).lower()
    if len(text) < 500:
        return PreflightResult(closed=False, reason="page too short to judge", checked=False)
    if not all(marker in text for marker in POSTING_MARKERS):
        return PreflightResult(
            closed=False, reason="page doesn't look like a job posting", checked=False
        )

    for phrase in CLOSED_PHRASES:
        if phrase in text:
            return PreflightResult(closed=True, reason=f'page says "{phrase}"', checked=True)

    return PreflightResult(closed=False, reason="no closed marker found", checked=True)
