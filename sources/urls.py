"""URL normalization shared by every source (and by cross-source dedup).

LinkedIn job links come in many shapes:

  https://www.linkedin.com/jobs/view/4479664903/
  https://www.linkedin.com/comm/jobs/view/4479664903?trackingId=...
  https://in.linkedin.com/jobs/view/data-engineer-at-acme-4479664903?source=jobfound.org
  http://linkedin.com/jobs/view/4479664903
  https://www.linkedin.com/jobs/view/senior-engineer-4479664903/?refId=...

All of them are stored as ``https://www.linkedin.com/jobs/view/<id>/``, which
is what module 1 has always stored.

Other apply links (Greenhouse, Ashby, Workday, iCIMS, careers pages) keep
their path and the query parameters the ATS needs; only known tracking
parameters (``source=jobfound.org``, ``utm_*``, ``gclid`` ...) are removed.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_LINKEDIN_JOB_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})(?:[/?#]|$)",
    re.I,
)

# Query parameters that only track the click. Compared lower-case. Anything
# not listed here is kept, because ATS links often need their parameters
# (Workday/iCIMS job ids, Greenhouse gh_jid, TalentBrew partner ids ...).
TRACKING_PARAMS = frozenset(
    {
        "source", "src", "ref", "refid", "trackingid", "trk", "trkinfo",
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
        "gclid", "gad_source", "dclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
        "_hsenc", "_hsmi", "lipi", "midtoken", "midsig", "eid", "otptoken",
    }
)
# `source=` (and Greenhouse's `gh_src=`) is stripped only when it names a job
# board, so an ATS that uses it as a real parameter keeps it unless it is a
# board tag. Greenhouse referral codes in gh_src are kept.
_BOARD_SOURCE_RE = re.compile(r"jobfound|linkedin|indeed|glassdoor|naukri", re.I)


def linkedin_job_id(url: str | None) -> Optional[str]:
    """The numeric LinkedIn job id in any LinkedIn job-view URL, else None."""
    m = _LINKEDIN_JOB_RE.match((url or "").strip())
    return m.group(1) if m else None


def normalize_linkedin_url(url: str | None) -> Optional[str]:
    """``https://www.linkedin.com/jobs/view/<id>/`` or None if not a LinkedIn job link."""
    jid = linkedin_job_id(url)
    return f"https://www.linkedin.com/jobs/view/{jid}/" if jid else None


def _is_tracking(key: str, value: str) -> bool:
    k = key.lower()
    if k in ("source", "src", "ref", "gh_src"):
        return bool(_BOARD_SOURCE_RE.search(value or ""))
    return k in TRACKING_PARAMS or k.startswith("utm_")


def normalize_apply_url(url: str | None) -> Optional[str]:
    """Normalized job URL for storage and dedup.

    LinkedIn job links -> canonical form. Other http(s) links -> scheme and
    host lower-cased, fragment dropped, tracking parameters removed, every
    other parameter kept in its original order. None for empty/non-http input.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    li = normalize_linkedin_url(raw)
    if li:
        return li
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k, v)]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query, doseq=True), "")
    )
