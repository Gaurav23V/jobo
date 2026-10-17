"""Domain circuit breaker for Module 4.

Some ATS platforms are structurally broken (403 on every request, infinite
loading spinner, protocol errors) rather than facing a form-filling problem
the agent could solve with more tries. Burning a full ~20-minute agent
session on the same dead domain 3 times per job, across many jobs, wastes
hours. This tracks hard-failure signatures per domain and short-circuits
future jobs on a tripped domain without spawning an agent at all.

State lives in a small JSON file next to the DB (not a DB migration - this
is operational/derived state, safe to delete and rebuild from scratch).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent.parent / "data" / "domain_circuit_breaker.json"

# Signatures that indicate a structurally broken destination, not a
# form-filling problem an agent could solve with more attempts.
HARD_FAILURE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b403\b.{0,20}forbidden",
        r"\b400\b.{0,30}try again later",
        r"ERR_HTTP2_PROTOCOL_ERROR",
        r"infinite (loading )?spinner",
        r"0 current openings",
        r"fails to load the job listing",
        r"portal is broken",
        r"is broken\b",
        r"cloudflare turnstile.{0,40}errors? out",
    ]
]

TRIP_THRESHOLD = 2  # distinct jobs hitting a hard-failure signature on this domain
TRIP_WINDOW_DAYS = 14  # ignore failures older than this when counting

# Job-board listing hosts. The runner passes job.job_url, and every queued job
# is a LinkedIn listing, so counting failures against linkedin.com lumps every
# employer's ATS together. On 2026-10-12 two unrelated failures (Coho's
# Ceipal portal, Redpine's site) tripped "linkedin.com", which would have
# dropped EVERY queued job permanently on the next run (it dropped 1485).
# The breaker only makes sense for the employer's own ATS host.
LISTING_HOSTS = ("linkedin.com",)

# Hosts one ATS shares across all its employers, with the employer in the
# first path segment (job-boards.greenhouse.io/<employer>/jobs/1). jobfound
# rows link to these directly, so the same lumping hazard as linkedin.com
# applies: two unrelated Greenhouse employers must not trip every Greenhouse
# job. On these hosts the breaker key is host/<employer>. Per-tenant hosts
# (acme.wd5.myworkdayjobs.com, careers-acme.icims.com) are already distinct.
SHARED_ATS_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "jobs.ashbyhq.com",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "ats.rippling.com",
    "apply.workable.com",
    "jobs.smartrecruiters.com",
)


def _domain(url: str) -> str:
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""
    if any(host == h or host.endswith("." + h) for h in LISTING_HOSTS):
        return ""
    if host in SHARED_ATS_HOSTS:
        segment = next((p for p in parts.path.split("/") if p), "").lower()
        if segment == "embed":  # boards.greenhouse.io/embed/job_app?for=<employer>
            segment = (parse_qs(parts.query).get("for") or [""])[0].lower()
        # No employer in the URL: no key, so the breaker never trips the whole host.
        return f"{host}/{segment}" if segment else ""
    return host


def is_hard_failure(detail: str) -> bool:
    return any(p.search(detail or "") for p in HARD_FAILURE_PATTERNS)


def _load() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        logger.warning("domain_circuit_breaker.json unreadable, starting fresh.")
        return {}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def record_failure(url: str, job_id: int, detail: str) -> None:
    """Record a hard-failure signature for the domain in `url`. No-op if
    `detail` doesn't match a known hard-failure pattern (soft/retryable
    failures like 'ran out of iterations' should never trip the breaker)."""
    if not is_hard_failure(detail):
        return
    domain = _domain(url)
    if not domain:
        return
    state = _load()
    entry = state.setdefault(domain, {"failures": [], "tripped_at": None})
    now = time.time()
    cutoff = now - TRIP_WINDOW_DAYS * 86400
    entry["failures"] = [f for f in entry["failures"] if f["ts"] >= cutoff]
    # Dedup by job_id so retries of the same job don't inflate the count.
    if not any(f["job_id"] == job_id for f in entry["failures"]):
        entry["failures"].append({"job_id": job_id, "ts": now, "detail": detail[:200]})
    distinct_jobs = {f["job_id"] for f in entry["failures"]}
    if len(distinct_jobs) >= TRIP_THRESHOLD and not entry["tripped_at"]:
        entry["tripped_at"] = now
        logger.warning(
            "Circuit breaker TRIPPED for domain %s after %d distinct jobs hit "
            "hard-failure signatures.",
            domain,
            len(distinct_jobs),
        )
    _save(state)


def is_tripped(url: str) -> str | None:
    """Return the tripping reason string if this URL's domain is circuit-broken,
    else None."""
    domain = _domain(url)
    if not domain:
        return None
    state = _load()
    entry = state.get(domain)
    if not entry or not entry.get("tripped_at"):
        return None
    last = entry["failures"][-1]["detail"] if entry["failures"] else "repeated hard failures"
    return f"domain '{domain}' circuit-broken ({len(entry['failures'])} hard failures): {last}"
