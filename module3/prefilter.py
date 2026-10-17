"""Post-enrich rule filter (`jobo prefilter`): drop rows before they cost a triage call.

Runs between enrich and triage for every source. Each enriched, not yet
triaged row is checked against policy/job_filters.py; a row that fails a rule
gets ``prefilter_reason`` and ``should_apply=False`` and module 3 never sees
it. Rules apply only when their inputs are known (job_filters decides that),
so a LinkedIn email row is judged on its title and an exact internship
employment type only.

Never touches a row that is scored, queued, applied, closed, already tried by
module 4 or already prefiltered.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.models import JobModel
from policy import job_filters

logger = logging.getLogger(__name__)

NOTE_PREFIX = "prefilter: "
# Rows per commit: short transactions, so the pipeline's other writers (and
# a reader like `jobo funnel`) never wait long on the jobs.db write lock.
COMMIT_EVERY = 50


@dataclass
class PrefilterResult:
    examined: int = 0
    dropped: int = 0
    kept: int = 0
    by_reason: Counter = field(default_factory=Counter)
    by_source: Counter = field(default_factory=Counter)  # dropped, per source_platform
    examined_by_source: Counter = field(default_factory=Counter)

    def render(self, *, dry_run: bool) -> str:
        head = "Prefilter (dry run, nothing written)" if dry_run else "Prefilter"
        lines = [f"{head}:", f"  Examined: {self.examined}", f"  Dropped:  {self.dropped}"]
        for reason, n in self.by_reason.most_common():
            lines.append(f"    {reason}: {n}")
        lines.append(f"  Kept:     {self.kept}")
        lines.append("  Per source (dropped / examined):")
        for src, n in sorted(self.examined_by_source.items()):
            lines.append(f"    {src}: {self.by_source.get(src, 0)} / {n}")
        return "\n".join(lines)


def eligible_rows(session: Session):
    """Enriched rows nothing downstream has touched yet."""
    return (
        session.query(JobModel)
        .filter(JobModel.module2_enriched_at.isnot(None))
        .filter(JobModel.module3_fit_score.is_(None))
        .filter(JobModel.should_apply.is_(None))
        .filter(JobModel.applied.isnot(True))
        .filter(JobModel.module4_closed_at.is_(None))
        .filter(or_(JobModel.module4_attempts.is_(None), JobModel.module4_attempts == 0))
        .filter(JobModel.prefilter_reason.is_(None))
        .order_by(JobModel.id)
    )


def run_prefilter(session: Session, *, dry_run: bool = False) -> PrefilterResult:
    result = PrefilterResult()
    pending = 0
    for job in eligible_rows(session).all():
        source = job.source_platform or "unknown"
        result.examined += 1
        result.examined_by_source[source] += 1
        reason = job_filters.drop_reason(job_filters.facts_from_row(job.job_title, job.metadata_json))
        if reason is None:
            result.kept += 1
            continue
        result.dropped += 1
        result.by_reason[reason] += 1
        result.by_source[source] += 1
        logger.info("job_id=%s dropped by prefilter: %s (%s)", job.id, reason, job.job_title)
        if dry_run:
            continue
        job.prefilter_reason = reason
        job.should_apply = False
        job.module3_fit_reasoning = f"{NOTE_PREFIX}{reason}"
        pending += 1
        if pending >= COMMIT_EVERY:
            session.commit()
            pending = 0
    if pending:
        session.commit()
    return result


def prefiltered_counts(session: Session) -> Counter:
    """Rows dropped by the prefilter, per reason (for funnel/doctor)."""
    rows = (
        session.query(JobModel.prefilter_reason)
        .filter(JobModel.prefilter_reason.isnot(None))
        .all()
    )
    return Counter(r[0] for r in rows)
