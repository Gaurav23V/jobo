"""The ONE insert path every source uses: cross-source dedup, then insert.

Contract (implemented under GHO-41 part (b); see docs/sources.md):

    insert_candidates(session, candidates, *, dry_run=False, now=None) -> InsertResult

For each ``JobCandidate`` (sources/base.py), in order:

  1. Dedup against the DB (and against earlier candidates in the same batch):
       a. primary: same normalized ``job_url`` (sources.urls.normalize_apply_url);
       b. secondary: same LinkedIn numeric job id, however the stored URL is
          written (in.linkedin vs www, slug vs bare id);
       c. fallback: same normalized company + normalized title, first seen
          within ``DEDUP_WINDOW_DAYS``. Never merges two rows whose LinkedIn
          ids are both known and differ (a repost is a different job).
  2. Duplicate: never inserted. The existing row gets ``last_seen_at = now``
     and the candidate's source appended to ``metadata_json['_sources']``.
     Nothing else on the existing row changes (apply state, scores,
     enrichment, should_apply, module4_* are untouched). A row whose
     ``metadata_json`` is not a JSON object only gets ``last_seen_at``.
  3. New: inserted with ``source_platform = candidate.source`` and
     ``metadata_json['_sources'] = [source]``. A pre-enriched candidate is
     inserted already enriched: ``metadata_json`` = its enrichment dict (plus
     ``_sources``), ``module2_attempted = True``, ``module2_enriched_at = now``,
     ``module2_model = candidate.enrichment_model``. A bare-link candidate is
     inserted un-enriched for module 2.

The dedup index (urls, LinkedIn ids, company+title) is loaded once per call.
Write transactions are short (one commit per candidate) so a collector can run
while the main pipeline holds the DB.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from db.models import JobModel
from sources import urls
from sources.base import SOURCES_KEY, JobCandidate

logger = logging.getLogger(__name__)

DEDUP_WINDOW_DAYS = 30

# Trailing legal-form tokens dropped from a normalized company name, longest
# first. Only legal forms: words like "technologies" or "solutions" are part
# of the name and two employers can differ only by them.
LEGAL_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("private", "limited"), ("private", "ltd"), ("pvt", "limited"), ("pvt", "ltd"),
    ("limited",), ("ltd",), ("llc",), ("llp",), ("inc",), ("incorporated",),
    ("corp",), ("corporation",), ("gmbh",), ("plc",), ("co",),
)

KIND_URL = "url"
KIND_LINKEDIN_ID = "linkedin_id"
KIND_COMPANY_TITLE = "company_title"


@dataclass
class InsertResult:
    seen: int = 0
    inserted: int = 0
    duplicates: int = 0
    # duplicates per match kind: "url", "linkedin_id", "company_title"
    duplicates_by: dict[str, int] = field(default_factory=dict)
    inserted_ids: list[int] = field(default_factory=list)
    # duplicates whose row has a metadata_json that is not a JSON object, so
    # the seen-in list was not touched (last_seen_at still updated)
    seen_in_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    """Naive UTC, the convention of every DateTime column in jobs.db."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_text(value: Optional[str]) -> str:
    """Lowercase, accents folded, punctuation -> space, spaces collapsed."""
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def normalize_company(value: Optional[str]) -> str:
    tokens = normalize_text(value).split()
    stripped = True
    while stripped:
        stripped = False
        for suffix in LEGAL_SUFFIXES:
            n = len(suffix)
            if len(tokens) > n and tuple(tokens[-n:]) == suffix:
                tokens = tokens[:-n]
                stripped = True
                break
    return " ".join(tokens)


def company_title_key(company: Optional[str], title: Optional[str]) -> Optional[tuple[str, str]]:
    c, t = normalize_company(company), normalize_text(title)
    return (c, t) if c and t else None


@dataclass
class _Entry:
    row_id: int
    first_seen: Optional[datetime]
    linkedin_id: Optional[str]


class DedupIndex:
    """Dedup keys of every known row, built once and grown as rows are added."""

    def __init__(self, window_days: int = DEDUP_WINDOW_DAYS):
        self.window = timedelta(days=window_days)
        self.by_url: dict[str, int] = {}
        self.by_linkedin_id: dict[str, int] = {}
        self.by_company_title: dict[tuple[str, str], list[_Entry]] = {}

    def add(self, row_id: int, job_url: Optional[str], company: Optional[str],
            title: Optional[str], first_seen: Optional[datetime]) -> None:
        # A stored LinkedIn URL is keyed as written, so "url" means the same
        # canonical link and a differently written one falls to "linkedin_id".
        raw = (job_url or "").strip()
        li = urls.linkedin_job_id(job_url)
        norm = raw if li else (urls.normalize_apply_url(job_url) or raw)
        if norm:
            self.by_url.setdefault(norm, row_id)
        if li:
            self.by_linkedin_id.setdefault(li, row_id)
        key = company_title_key(company, title)
        if key:
            self.by_company_title.setdefault(key, []).append(_Entry(row_id, first_seen, li))

    def _company_title_entries(self, li: Optional[str], key, now: datetime):
        """Rows in the window with this key: (mergeable, kept apart by a LinkedIn id)."""
        cutoff = now - self.window
        ok, apart = [], []
        for e in self.by_company_title.get(key, ()):
            if e.first_seen is None or e.first_seen < cutoff:
                continue
            (apart if li and e.linkedin_id and e.linkedin_id != li else ok).append(e)
        return ok, apart

    def match(self, job_url: Optional[str], company: Optional[str], title: Optional[str],
              now: datetime) -> Optional[tuple[int, str]]:
        """(existing row id, match kind) or None for a new job."""
        norm = urls.normalize_apply_url(job_url)
        if norm and norm in self.by_url:
            return self.by_url[norm], KIND_URL
        li = urls.linkedin_job_id(job_url)
        if li and li in self.by_linkedin_id:
            return self.by_linkedin_id[li], KIND_LINKEDIN_ID
        key = company_title_key(company, title)
        if not key:
            return None
        ok, _ = self._company_title_entries(li, key, now)
        if not ok:
            return None
        return max(ok, key=lambda e: e.first_seen).row_id, KIND_COMPANY_TITLE

    def company_title_kept_apart(self, job_url: Optional[str], company: Optional[str],
                                 title: Optional[str], now: datetime) -> bool:
        """True when a company+title match in the window was refused for a differing LinkedIn id."""
        key = company_title_key(company, title)
        if not key:
            return False
        _, apart = self._company_title_entries(urls.linkedin_job_id(job_url), key, now)
        return bool(apart)


def load_index(session: Session, window_days: int = DEDUP_WINDOW_DAYS) -> DedupIndex:
    index = DedupIndex(window_days)
    rows = session.query(
        JobModel.id, JobModel.job_url, JobModel.company_name, JobModel.job_title, JobModel.first_seen_at
    ).order_by(JobModel.id)
    for row_id, job_url, company, title, first_seen in rows:
        index.add(row_id, job_url, company, title, first_seen)
    return index


def _mark_seen(session: Session, row_id: int, source: str, now: datetime) -> bool:
    """last_seen_at + seen-in list on an existing row. False if metadata was left alone."""
    row = session.get(JobModel, row_id)
    if row is None:
        raise LookupError(f"row {row_id} vanished")
    row.last_seen_at = now
    try:
        meta = json.loads(row.metadata_json or "{}")
    except (TypeError, ValueError):
        meta = None
    touched = isinstance(meta, dict)
    if touched:
        seen_in = meta.get(SOURCES_KEY)
        if isinstance(seen_in, list):
            seen_in = list(seen_in)
        else:
            seen_in = [row.source_platform] if row.source_platform else []
        if source not in seen_in:
            seen_in.append(source)
        if meta.get(SOURCES_KEY) != seen_in:
            meta[SOURCES_KEY] = seen_in
            row.metadata_json = json.dumps(meta, default=str, ensure_ascii=False)
    session.commit()
    return touched


def _new_row(candidate: JobCandidate, job_url: str, now: datetime) -> JobModel:
    meta = dict(candidate.enrichment) if candidate.pre_enriched else {}
    meta[SOURCES_KEY] = [candidate.source]
    row = JobModel(
        company_name=candidate.company_name,
        job_title=candidate.job_title,
        location=candidate.location,
        date_released=candidate.date_released,
        applied=False,
        referred_json="[]",
        job_url=job_url,
        source_platform=candidate.source,
        first_seen_at=now,
        last_seen_at=now,
        metadata_json=json.dumps(meta, default=str, ensure_ascii=False),
        module2_attempted=False,
    )
    if candidate.pre_enriched:
        row.module2_attempted = True
        row.module2_enriched_at = now
        row.module2_model = candidate.enrichment_model
        row.module2_last_error = candidate.enrichment_error
    return row


def insert_candidates(
    session: Session,
    candidates: Iterable[JobCandidate],
    *,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> InsertResult:
    now = now or utcnow()
    result = InsertResult()
    index = load_index(session)
    dry_id = 0  # stand-in ids for dry-run inserts, so in-batch dedup still works

    for cand in candidates:
        result.seen += 1
        try:
            job_url = urls.normalize_apply_url(cand.job_url)
            if not job_url:
                raise ValueError(f"unusable job_url {cand.job_url!r}")
            hit = index.match(job_url, cand.company_name, cand.job_title, now)
            if hit:
                row_id, kind = hit
                result.duplicates += 1
                result.duplicates_by[kind] = result.duplicates_by.get(kind, 0) + 1
                if not dry_run and not _mark_seen(session, row_id, cand.source, now):
                    result.seen_in_skipped += 1
                continue
            if dry_run:
                dry_id -= 1
                row_id = dry_id
            else:
                row = _new_row(cand, job_url, now)
                session.add(row)
                session.commit()
                row_id = row.id
                result.inserted_ids.append(row_id)
            result.inserted += 1
            index.add(row_id, job_url, cand.company_name, cand.job_title, now)
        except Exception as e:  # one bad candidate must not stop the batch
            session.rollback()
            logger.error("insert_candidates: %s candidate %s failed: %s", cand.source, cand.job_url, e)
            result.errors.append(f"{cand.source} {cand.job_url}: {e}")
    return result
