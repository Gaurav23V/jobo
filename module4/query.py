"""Query jobs eligible for Module 4 submission."""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import JobModel
from module4 import address_wall, application_caps, captcha_walls, email_apply, persist
from policy import employer_blocklist, paused_sites


def list_jobs_to_apply(
    session: Session,
    *,
    limit: int | None = None,
    force: bool = False,
    job_id: int | None = None,
    retire_blocked: bool = False,
) -> list[JobModel]:
    """should_apply=true rows without a successful submission (unless force).

    Rows where applied=true are skipped; rows with module4_last_status set but
    applied still false are retried (transient failures).

    job_id: restrict to a single job — used for one-off testing without
    pulling from the full apply backlog.

    Blocked employers (policy.employer_blocklist) are removed unconditionally,
    including under --force and --job-id. The blocklist is a standing user
    preference, not a recoverable failure state.

    retire_blocked: also write each blocked row's terminal state
    (persist.retire_blocked) so it leaves the queue for good. The apply
    callers pass it on a real run; read-only callers (plan, tests) do not.
    """
    q = (
        session.query(JobModel)
        .filter(JobModel.should_apply.is_(True))
        .filter(JobModel.applied.isnot(True))
        # Dead listings are terminal and send nothing. They used to be hidden
        # by applied=1 (wrongly, see module4/persist.mark_closed); this is the
        # explicit filter that replaces that accident.
        .filter(JobModel.module4_closed_at.is_(None))
        .filter(JobModel.module2_enriched_at.isnot(None))
        # Backstop: a prefiltered row already has should_apply=False, and
        # only a triage verdict (which clears prefilter_reason) can set it.
        .filter(JobModel.prefilter_reason.is_(None))
        .order_by(JobModel.module3_fit_score.desc(), JobModel.id)
    )
    if job_id is not None:
        q = q.filter(JobModel.id == job_id)
    # Blocklist filtering happens BEFORE `limit` so a blocked job cannot eat a
    # slot in a capped run (JOBO_APPLY_MAX_JOBS).
    rows = q.all()
    # Blocked first, so a blocked job parked on a paused site is retired too.
    rows, blocked = employer_blocklist.partition(rows)
    if blocked:
        employer_blocklist.log_blocked(blocked, "apply")
        if retire_blocked:
            for job in blocked:
                persist.retire_blocked(session, job, employer_blocklist.job_block_reason(job) or "blocklisted")
    # Jobs parked on a paused ATS wait until that site leaves
    # policy/paused_sites.PAUSED_SITES; an explicit --job-id or --force still
    # reaches them, for testing a fix.
    # Jobs parked behind an hCaptcha sign-in gate (module4/captcha_walls.py)
    # wait the same way, and jobs on a host whose application cap is cooling
    # down (module4/application_caps.py) wait for the cool-down to end.
    if job_id is None and not force:
        rows = [
            r for r in rows
            if not paused_sites.still_paused(r.module4_last_status)
            and not captcha_walls.is_parked(r.module4_last_status)
            # A required street address / postal code (address_wall.py).
            and not address_wall.is_parked(r.module4_last_status)
            and not application_caps.is_parked(r.module4_last_status)
            # A parked email draft waits for draft mode to end (email_apply.py).
            and not (email_apply.is_parked(r.module4_last_status) and email_apply.mode() != "send")
        ]
    if limit:
        rows = rows[:limit]
    return rows
