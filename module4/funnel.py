"""The apply-rate funnel metric (GHO-2).

Pinned definition: of jobs with should_apply=1, first seen in a window, and
still open (listing not closed), the share that reached `submitted`.

"Still open" excludes outcome=closed from the denominator: a dead listing
was never reachable, so it is not a miss against the 95% target the same
way a stuck/not-yet-attempted job is. Closed jobs are still reported, just
outside the rate's denominator, so a reader can see how many good-fit jobs
were lost to closures versus to our own funnel.

first_seen_at is stamped by our own collector at insert time (module1), so
"first seen in a window" and "a single pipeline run's window" are the same
kind of filter: a run's wall-clock [start, end) is exactly the first_seen_at
range of jobs it could have collected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_
from sqlalchemy.orm import Session

from db.models import JobModel

LOG_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


@dataclass
class FunnelResult:
    since: datetime | None
    until: datetime | None
    should_apply_total: int
    closed: int
    still_open: int
    submitted: int
    stuck: int
    skipped_by_policy: int
    not_attempted: int
    apply_rate: float | None  # None when still_open == 0 (undefined, not 0%)
    # Rows first seen in the window that the post-enrich rule filter dropped
    # before triage (module3/prefilter.py), per reason. Outside the rate: they
    # never had should_apply=1.
    prefiltered: dict[str, int] = field(default_factory=dict)

    def render(self, title: str) -> str:
        lines = [title]
        window = f"{self.since.isoformat() if self.since else '-inf'} .. {self.until.isoformat() if self.until else 'now'}"
        lines.append(f"  Window (first_seen_at):        {window}")
        lines.append(f"  should_apply=1, first seen here: {self.should_apply_total}")
        lines.append(f"    closed (listing died, excluded from rate): {self.closed}")
        lines.append(f"    still open (the rate's denominator):       {self.still_open}")
        lines.append(f"      submitted:          {self.submitted}")
        lines.append(f"      stuck:              {self.stuck}")
        lines.append(f"      skipped_by_policy:  {self.skipped_by_policy}")
        lines.append(f"      not_attempted:      {self.not_attempted}")
        if self.apply_rate is None:
            lines.append("  APPLY RATE: undefined (no still-open jobs in this window)")
        else:
            lines.append(
                f"  APPLY RATE: {self.apply_rate:.1%}  ({self.submitted}/{self.still_open})"
            )
        total = sum(self.prefiltered.values())
        lines.append(f"  Prefiltered before triage (not in the rate): {total}")
        for reason, n in sorted(self.prefiltered.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {reason}: {n}")
        return "\n".join(lines)


def compute_funnel(
    session: Session, *, since: datetime | None = None, until: datetime | None = None
) -> FunnelResult:
    q = session.query(JobModel).filter(JobModel.should_apply == True)  # noqa: E712
    if since is not None:
        q = q.filter(JobModel.first_seen_at >= since)
    if until is not None:
        q = q.filter(JobModel.first_seen_at < until)
    rows = q.all()

    closed = sum(1 for r in rows if r.module4_outcome == "closed")
    submitted = sum(1 for r in rows if r.module4_outcome == "submitted")
    stuck = sum(1 for r in rows if r.module4_outcome == "stuck")
    skipped_by_policy = sum(1 for r in rows if r.module4_outcome == "skipped_by_policy")
    not_attempted = sum(
        1 for r in rows if r.module4_outcome in (None, "not_attempted")
    )
    still_open = len(rows) - closed
    rate = (submitted / still_open) if still_open else None

    pq = session.query(JobModel.prefilter_reason).filter(JobModel.prefilter_reason.isnot(None))
    if since is not None:
        pq = pq.filter(JobModel.first_seen_at >= since)
    if until is not None:
        pq = pq.filter(JobModel.first_seen_at < until)
    prefiltered: dict[str, int] = {}
    for (reason,) in pq:
        prefiltered[reason] = prefiltered.get(reason, 0) + 1

    return FunnelResult(
        since=since,
        until=until,
        should_apply_total=len(rows),
        closed=closed,
        still_open=still_open,
        submitted=submitted,
        stuck=stuck,
        skipped_by_policy=skipped_by_policy,
        not_attempted=not_attempted,
        apply_rate=rate,
        prefiltered=prefiltered,
    )


def run_window_from_log(log_path: Path) -> tuple[datetime, datetime]:
    """A pipeline run's [start, end] wall-clock window from its log file.

    scripts/daily_pipeline.sh's log() helper prefixes every line with
    "[%F %T]"; the first and last such timestamps bound the run.
    """
    first: str | None = None
    last: str | None = None
    with log_path.open() as f:
        for line in f:
            m = LOG_TIMESTAMP_RE.match(line)
            if not m:
                continue
            if first is None:
                first = m.group(1)
            last = m.group(1)
    if first is None or last is None:
        raise ValueError(f"no timestamped log lines found in {log_path}")
    fmt = "%Y-%m-%d %H:%M:%S"
    start = datetime.strptime(first, fmt)
    end = datetime.strptime(last, fmt)
    return start, end
