"""`jobo apply --plan`: what the apply stage would do with each queued job, with no side effects.

No browser, no agent, no network, no DB writes. Every column comes from the
same function the runner and the parallel queue call (query.list_jobs_to_apply,
employer_blocklist, circuit_breaker, blockers, paused_sites, preflight's URL
test, routing.initial_route), so the report is the apply stage's own decision,
up to the point where a browser or an agent would be needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from db.models import JobModel
from module4 import blockers, circuit_breaker, constants, preflight, query, routing
from policy import employer_blocklist, paused_sites

WORKER_LINKEDIN_ROUTES = "linkedin worker (routes the listing)"
WORKER_FORM = "form worker (direct)"
WORKER_LINKEDIN_DIRECT = "linkedin worker (direct)"


@dataclass
class PlanRow:
    job_id: int
    source: str
    kind: str
    blocklisted: str | None
    paused: str | None
    breaker: str | None
    not_retryable: str | None
    linkedin_session_check: bool
    preflight: bool
    worker: str
    queued: bool = True
    filtered: str | None = None  # why list_jobs_to_apply leaves it out

    @property
    def verdict(self) -> str:
        if self.filtered:
            return f"NOT QUEUED: {self.filtered}"
        for label, value in (
            ("blocked employer", self.blocklisted),
            ("circuit-broken", self.breaker),
            ("not retryable", self.not_retryable),
            ("paused site", self.paused),
        ):
            if value:
                return f"skip ({label})"
        return "apply"


def _filtered_reason(job: JobModel) -> str:
    """Why list_jobs_to_apply does not return this row (same filters, in order)."""
    if not job.should_apply:
        if job.prefilter_reason:
            return f"prefiltered ({job.prefilter_reason})"
        return "should_apply is not true"
    if job.applied:
        return "already applied"
    if job.module4_closed_at is not None:
        return "listing closed"
    if job.module2_enriched_at is None:
        return "not enriched"
    if job.prefilter_reason:
        return f"prefiltered ({job.prefilter_reason})"
    reason = employer_blocklist.job_block_reason(job)
    if reason:
        return f"blocked employer ({reason})"
    return "filtered by the apply queue"


def plan_row(job: JobModel, *, force: bool = False, filtered: str | None = None) -> PlanRow:
    url = str(job.job_url or "")
    route, _, _ = routing.initial_route(url)
    worker = {
        None: WORKER_LINKEDIN_ROUTES,
        "form": WORKER_FORM,
        "linkedin": WORKER_LINKEDIN_DIRECT,
    }[route]
    paused = paused_sites.paused_site_for_url(url)
    if not paused and paused_sites.still_paused(job.module4_last_status):
        paused = f"parked: {job.module4_last_status}"
    not_retryable = None
    if (job.module4_attempts or 0) > 0 and not force:
        not_retryable = blockers.permanent_reason_from_last_status(job.module4_last_status)
    return PlanRow(
        job_id=job.id,
        source=job.source_platform or "unknown",
        kind=routing.ats_kind(url),
        blocklisted=employer_blocklist.job_block_reason(job),
        paused=paused,
        breaker=circuit_breaker.is_tripped(url),
        not_retryable=not_retryable,
        linkedin_session_check=routing.is_linkedin_listing(url),
        preflight=preflight._is_linkedin_job_url(url),
        worker=worker,
        queued=filtered is None,
        filtered=filtered,
    )


def missing_columns() -> list[str]:
    """JobModel columns the DB does not have yet (a DB from before a migration)."""
    from sqlalchemy import inspect

    from db.database import engine

    have = {c["name"] for c in inspect(engine).get_columns("jobs")}
    return [c.name for c in JobModel.__table__.columns if c.name not in have]


def build_plan(
    session: Session, *, job_id: int | None = None, limit: int | None = None, force: bool = False
) -> list[PlanRow]:
    jobs = query.list_jobs_to_apply(session, limit=limit, force=force, job_id=job_id)
    cap = constants.max_jobs_per_run()
    if cap and not limit:
        jobs = jobs[:cap]
    rows = [plan_row(j, force=force) for j in jobs]
    if job_id is not None and not rows:
        job = session.get(JobModel, job_id)
        if job is None:
            return []
        rows.append(plan_row(job, force=force, filtered=_filtered_reason(job)))
    return rows


def render(rows: list[PlanRow]) -> str:
    if not rows:
        return "Apply plan: no jobs."
    yn = lambda b: "yes" if b else "no"  # noqa: E731
    lines = [f"Apply plan ({sum(r.queued for r in rows)} queued; nothing was opened, run or written):"]
    for r in rows:
        lines.append(
            f"  job {r.job_id}: source={r.source} kind={r.kind} "
            f"blocklisted={yn(r.blocklisted)} paused={yn(r.paused)} "
            f"linkedin_session_check={yn(r.linkedin_session_check)} preflight={yn(r.preflight)} "
            f"worker={r.worker} -> {r.verdict}"
        )
        for label, value in (("blocklist", r.blocklisted), ("paused", r.paused),
                             ("breaker", r.breaker), ("not retryable", r.not_retryable)):
            if value:
                lines.append(f"      {label}: {value}")
    kinds: dict[str, int] = {}
    for r in rows:
        if r.queued:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
    if kinds:
        lines.append("  By kind: " + ", ".join(f"{k}={n}" for k, n in sorted(kinds.items())))
    return "\n".join(lines)
