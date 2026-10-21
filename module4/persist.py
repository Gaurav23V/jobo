"""Persist Module 4 submission state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from db.models import JobModel
from module4.outcome_taxonomy import classify_outcome


def _stamp_outcome(job: JobModel) -> None:
    job.module4_outcome = classify_outcome(
        applied=bool(job.applied), last_status=job.module4_last_status
    )


def mark_applied(
    session: Session, job: JobModel, *, dry_run: bool = False, status: str = "submitted"
) -> None:
    """A real application was sent for this job.

    ONLY call this for a genuine submission. A dead listing is terminal too,
    but it sent nothing - use mark_closed() for that. `status` may carry a
    receipt after "submitted:" (an emailed application's Gmail message id).
    """
    job.applied = True
    job.module4_last_status = status
    job.module4_submitted_at = datetime.now()
    _stamp_outcome(job)
    if not dry_run:
        session.add(job)
        session.commit()


def mark_closed(
    session: Session,
    job: JobModel,
    *,
    status: str = "closed",
    detail: str = "",
    dry_run: bool = False,
) -> None:
    """Terminal, but NOTHING was submitted: the listing is dead/already handled.

    This used to call mark_applied(), which hardcodes status='submitted' and
    stamps module4_submitted_at - so 67 dead listings ended up indistinguishable
    from real applications in the DB, and every total reported to the user was
    inflated. The run summary had been fixed to count these separately on
    2026-09-02; the DB write was missed.

    `applied` stays False (nothing was sent). `should_apply` goes False and
    module4_closed_at is stamped so the job leaves the apply queue permanently
    and can still be told apart from an untouched pending row.
    """
    job.applied = False
    job.should_apply = False
    job.module4_closed_at = datetime.now()
    job.module4_submitted_at = None
    job.module4_last_status = f"{status}:{detail[:600]}" if detail else status
    _stamp_outcome(job)
    if not dry_run:
        session.add(job)
        session.commit()


def mark_status(
    session: Session,
    job: JobModel,
    status: str,
    *,
    dry_run: bool = False,
    count_as_attempt: bool = False,
) -> None:
    """Transient status: closed / external_failed:<reason> / failed:<reason>.

    count_as_attempt: bump module4_attempts too. Used for failures that are
    genuinely retryable-but-currently-blocked (e.g. missing_resume) so a
    permanently-broken precondition still eventually hits max_attempts and
    gets dropped, instead of silently retrying forever at attempts=0.
    """
    job.module4_last_status = status
    if status == "closed":
        # A dead posting is terminal: nothing to retry later.
        job.should_apply = False
    if count_as_attempt:
        job.module4_attempts = (job.module4_attempts or 0) + 1
    _stamp_outcome(job)
    if not dry_run:
        session.add(job)
        session.commit()


def record_failed_attempt(
    session: Session,
    job: JobModel,
    outcome,
    *,
    dry_run: bool = False,
) -> int:
    """Bump the attempt counter and store the failure detail.

    Returns the new attempt count so the runner can drop jobs that have
    exhausted their retries.
    """
    job.module4_attempts = (job.module4_attempts or 0) + 1
    # Keep enough of the reason for `_is_budget_exhaustion` to work. At 150
    # chars the deciding phrase was routinely cut off: job489's failure was
    # "...the final Apply submit button click kept failing due to an overlay
    # div intercepting clicks, and I ran out of turns before..." - the "ran out
    # of" marker sat past the cutoff, so the retry got the SHORT budget (480s)
    # when it should have been escalated to 30min, and it timed out again.
    # The full transcript still lives in data/logs/apply_debug/.
    job.module4_last_status = f"{outcome.status}:{outcome.detail[:600]}"
    _stamp_outcome(job)
    if not dry_run:
        session.add(job)
        session.commit()
    return job.module4_attempts


def drop_permanently(
    session: Session,
    job: JobModel,
    *,
    dry_run: bool = False,
) -> None:
    """Take a retry-exhausted job out of the apply queue for good.

    module4_last_status keeps the last failure reason so the user can review these
    entries later; should_apply=0 just stops the pipeline from retrying.
    """
    job.should_apply = False
    _stamp_outcome(job)
    if not dry_run:
        session.add(job)
        session.commit()
