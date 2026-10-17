"""Hard employer blocklist - companies we must NEVER apply to.

Why this exists
---------------
Some employers and sites the candidate never wants to apply to, whatever the
fit score says. The list itself is personal (personal/candidate.json, see
below); this module only enforces it.

This is a standing preference, not a failure mode, so it is deliberately NOT
implemented as a retry rule, a circuit breaker, or a fit-score threshold. Those
are all recoverable - a `--force`, a reset script, or a high fit score can
undo them. A blocklist entry must be unconditional.

Design rules
------------
1. **Refuse, don't repair.** Blocked jobs are never triaged and never applied
   to, regardless of `should_apply`, `--force`, `--job-id`, fit score, or
   remaining attempts. `--job-id` on a blocked employer is an explicit user
   request and still refuses, loudly - that is the point of a hard limit.
2. **Match on more than one field.** One employer can appear under several
   `company_name` spellings ("Globex", "Globex in India", "Globex Services Pvt
   Ltd"), and a posting can carry an ATS URL on the
   employer's own domain while the company_name says something else entirely
   (an agency, a subsidiary). Both are checked.
3. **Match tokens, not bare substrings.** The recurring bug class in this
   repo is a pattern that matches something incidental (see
   `reset_attempts.py`'s `phone|sms` matching a list of successfully filled
   fields). A blocklist entry is checked against word-ish boundaries so a
   hypothetical "Globexly Ltd" or a URL like `notglobex.example` does not
   silently get blocked, while every real spelling does.
4. **Extensible without a code change.** `JOBO_BLOCKED_EMPLOYERS` (comma
   separated) adds entries at runtime. The configured list is the floor - env
   input can only ever ADD, never remove, so a stray empty env var cannot
   quietly disable the block.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from policy import personal

logger = logging.getLogger(__name__)

# Which employers and sites to refuse is the candidate's own choice, so the
# list lives in the gitignored personal/candidate.json, never in the code:
#     "blocked_employers": ["Globex"],            # company-name tokens
#     "blocked_domains":   ["globex.example"]     # employer sites and job boards
# A domain entry also covers its subdomains. A job board (an aggregator, never
# an employer) belongs in blocked_domains only, so a company that merely shares
# its name is not blocked. JOBO_BLOCKED_EMPLOYERS (comma separated) can add to
# the configured list for one run, never remove from it.


def _config_list(key: str) -> tuple[str, ...]:
    raw = personal.get(key, []) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return tuple(str(x).strip().lower() for x in raw if str(x).strip())


def configured_employers() -> tuple[str, ...]:
    return _config_list("blocked_employers")


def blocked_domains() -> tuple[str, ...]:
    return _config_list("blocked_domains")


def prompt_text() -> str:
    """The blocklisted-destination line for the apply agent's prompt."""
    names = list(dict.fromkeys(configured_employers()))
    doms = list(dict.fromkeys(blocked_domains()))
    if not names and not doms:
        return "Blocklisted: none configured."
    parts = [f"**{d}** (and any subdomain)" for d in doms]
    if names:
        parts.append("any employer named " + ", ".join(f"**{n}**" for n in names))
    return "Blocklisted: " + "; ".join(parts) + "."


# Destinations that are blocked wherever their domain appears: in a job URL, in
# the agent's stored failure text, or on the page the agent lands on.
_DOMAIN_IN_TEXT_RE_CACHE: dict[str, re.Pattern] = {}


def _domain_in_text_re(dom: str) -> re.Pattern:
    pat = _DOMAIN_IN_TEXT_RE_CACHE.get(dom)
    if pat is None:
        # "board.example", "www.board.example", "https://www.board.example/..." all
        # hit; "myboard.example" and "board.example.org" do not.
        pat = re.compile(rf"(?<![a-z0-9-]){re.escape(dom)}(?![a-z0-9-])", re.I)
        _DOMAIN_IN_TEXT_RE_CACHE[dom] = pat
    return pat


def destination_block_reason(text: Optional[str]) -> Optional[str]:
    """A reason when free text (a stored module4_last_status, an agent DETAIL)
    shows the apply flow going through a blocked domain, else None."""
    if not text:
        return None
    # Matched with the dot, so the plain word "board" never triggers this.
    for dom in blocked_domains():
        if _domain_in_text_re(dom).search(text):
            return f"apply flow goes through the blocked site {dom!r}"
    return None

_ENV_VAR = "JOBO_BLOCKED_EMPLOYERS"


def _env_entries() -> list[str]:
    raw = os.environ.get(_ENV_VAR, "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def blocked_terms() -> list[str]:
    """The effective blocklist: built-in floor plus any env additions."""
    terms = list(configured_employers())
    for extra in _env_entries():
        if extra not in terms:
            terms.append(extra)
    return terms


def _token_match(haystack: str, term: str) -> bool:
    """True when `term` appears in `haystack` on word-ish boundaries.

    Guards against the incidental-substring bug: "globex" must match
    "Globex in India" and "Globex Services Pvt Ltd", but not a longer
    unrelated word that merely contains it.
    """
    if not haystack:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack.lower()) is not None


def _domain_of(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    except ValueError:
        return ""
    return host.lower()


def _domain_blocked(url: str) -> Optional[str]:
    host = _domain_of(url)
    if not host:
        return None
    for dom in blocked_domains():
        # Exact host or a subdomain of it - never a suffix match on a
        # different registrable domain (`notglobex.example` must not hit).
        if host == dom or host.endswith(f".{dom}"):
            return dom
    return None


def block_reason(
    company_name: Optional[str] = None, job_url: Optional[str] = None
) -> Optional[str]:
    """Return a human-readable reason if this job is blocked, else None."""
    company = (company_name or "").strip()
    for term in blocked_terms():
        if _token_match(company, term):
            return f"employer {company!r} is on the hard blocklist (matched {term!r})"
    dom = _domain_blocked(job_url or "")
    if dom:
        return f"job URL is on the blocked domain {dom!r} (company_name={company!r})"
    return None


def is_blocked(
    company_name: Optional[str] = None, job_url: Optional[str] = None
) -> bool:
    return block_reason(company_name, job_url) is not None


def job_block_reason(job) -> Optional[str]:
    """block_reason for a JobModel-shaped object, plus its stored apply status:
    a job whose last attempt landed on a blocked destination (a blocked job board) is
    blocked even though its company_name and LinkedIn job_url look normal."""
    reason = block_reason(getattr(job, "company_name", None), getattr(job, "job_url", None))
    if reason:
        return reason
    return destination_block_reason(getattr(job, "module4_last_status", None))


def is_blocked_job(job) -> bool:
    """Convenience for a JobModel-shaped object."""
    return job_block_reason(job) is not None


def partition(jobs: Iterable) -> tuple[list, list]:
    """Split an iterable of JobModel rows into (allowed, blocked)."""
    allowed, blocked = [], []
    for j in jobs:
        (blocked if is_blocked_job(j) else allowed).append(j)
    return allowed, blocked


def log_blocked(jobs: Iterable, stage: str) -> int:
    """Log each blocked job once. Returns how many were blocked."""
    n = 0
    for j in jobs:
        reason = job_block_reason(j)
        if reason:
            n += 1
            logger.info(
                "[%s] BLOCKED EMPLOYER, skipping job %s: %s",
                stage,
                getattr(j, "id", "?"),
                reason,
            )
    return n
