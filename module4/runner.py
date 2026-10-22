"""Module 4 runner: sequential apply loop with human-like pacing.

Browser lifecycle: one persistent, CDP-attached Chromium instance serves the
whole run (module4.browser_session), started once before the loop and left
running for the next `hermes chat` subprocess call to attach to via
BROWSER_CDP_URL. Tabs are swept between jobs so a long run doesn't accumulate
dozens of open tabs.

Circuit breaker: domains that have hit a hard-failure signature (403, broken
portal, protocol error) on 2+ distinct jobs are skipped without spawning an
agent at all - see module4.circuit_breaker for the rationale.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from db.models import JobModel
from module4 import (
    agent_driver,
    blockers,
    browser_session,
    budget_guard,
    captcha_walls,
    circuit_breaker,
    constants,
    email_apply,
    frame_keeper,
    google_forms,
    linkedin_router,
    linkedin_session,
    persist,
    preflight,
    prompts,
    query,
    routing,
    submission_audit,
    upload_names,
)
from policy import employer_blocklist, paused_sites

logger = logging.getLogger(__name__)

# Failure signatures meaning "the agent ran out of room", not "this job is
# impossible". A retry after one of these needs MORE budget, not less - the
# old behaviour halved it, which guaranteed a repeat. Observed on 2026-09-02:
# job462 attached the resume successfully and then hit the turn cap on
# ordinary text fields; several others reached 90-95% of a form.
_BUDGET_EXHAUSTION_MARKERS = (
    "tool-call",
    "tool call",
    "iteration",
    "max turns",
    "turns",
    "budget",
    "timed out",
    "ran out of",
    "no verdict printed",
)


def _is_budget_exhaustion(status: str | None) -> bool:
    s = (status or "").lower()
    return any(marker in s for marker in _BUDGET_EXHAUSTION_MARKERS)


# Failures caused by OUR infrastructure being broken, not by the job. These
# must NOT consume one of the job's three attempts - on the 2026-09-03 run a
# dead browser burned a real attempt on 36 jobs in ten minutes flat, several
# of which were one attempt away from being permanently dropped.
#
# Match the CONDITION, never a bare component name: "browser" or "cdp" alone
# would swallow genuine site failures that merely mention them. Every entry
# here describes OUR side being unreachable or unresponsive.
#
# Each new wording an agent invents has to be added as it is observed - the
# agent writes this prose freely, so the list is inherently incomplete. On
# 2026-09-11 job611 (Apple) failed with "Browser session unresponsive at CDP
# layer, navigate/snapshot/vision all timed out" and matched NOTHING, so a
# browser fault was charged to the job as if the posting were at fault.
_INFRA_FAILURE_MARKERS = (
    "cdp connection",
    "connection refused",
    "browser backend unreachable",
    "browser tooling",
    "os error 111",
    "no chrome instance",
    "browser backend (cdp",
    "browser tool backend",
    "tab is not responding",
    "tab is unresponsive",
    # Added 2026-09-11 after job611 was wrongly charged an attempt.
    "browser session unresponsive",
    "browser session became unresponsive",
    "browser became unresponsive",
    "browser instance became unresponsive",
    "browser is unresponsive",
    "unresponsive at cdp",
    "cdp layer",
    "browser session crashed",
    "browser crashed",
    "browser backend lost connection",
    "lost connection to the browser",
    "browser disconnected",
    # Added 2026-09-15: the browser was fine, but the MODEL PROVIDER was not.
    # A ~6-minute outage at 03:17 killed 18 consecutive jobs (Google, Merlin,
    # Eastlake, Alpineer among them). The agent process died before printing a
    # verdict, so each one persisted as a bare `stuck:` and was charged a real
    # attempt for an outage on our side. The apply stage cannot run at all
    # without the model, so this belongs in the same class as a dead browser.
    "can't reach the model provider",
    "cannot reach the model provider",
    "api call failed after",
    "model provider",
    "you may be offline",
    "agent produced no verdict",
    "agent produced no output",
    # Hermes's wording when a provider fails every retry (2026-09-26).
    "didn't answer after",
)

# The model account is out of quota: nothing will work until it resets, so
# the stage stops at the FIRST one of these instead of walking the queue.
# On 2026-09-26 Claude's session limit hit mid-run and 101 jobs failed
# against it one after another.
_USAGE_LIMIT_MARKERS = (
    "hit your session limit",
    "hit your weekly limit",
    "hit your usage limit",
    "hit your limit",
    "exceed your account's rate limit",
    "free period has ended",
    "no longer free",
)

# Jobs in a row on one worker that show the marks of an agent driving some
# other browser (GHO-17): it says LinkedIn is signed out while the feed probe
# says signed in, or an upload finds no file input in any tab of our browser.
# The 15 Oct 12:40 run had 21 of the first in a row and nothing tripped.
MAX_ATTACH_MISMATCHES = 3

# Consecutive infrastructure failures (model provider or browser) after which
# the stage stops. The browser path already stopped on an unrecoverable
# browser; a dead MODEL with a healthy browser used to grind on - 2026-09-26.
MAX_CONSECUTIVE_INFRA_FAILURES = 3

# Agents in a row that report a LinkedIn sign-in wall while our own probe of
# the same browser says it is signed in. One is a misread page; several mean
# the agent is not driving this browser at all. On 15 Oct 2026 (GHO-13) a
# Hermes update had every agent in a blank local browser, and 24 such
# verdicts went by, each ~20s, before a person stopped the run.
MAX_CONSECUTIVE_SIGN_IN_MISREADS = 3


# While paused for the 5-hour session window to reset (GHO-22), how often to
# look at the run's stop flag. Clock seams: tests drive a fake clock.
SESSION_PAUSE_POLL_SEC = 30.0
_monotonic = time.monotonic
_wall = time.time
_sleep = time.sleep


# Post-submission pacing, per apply host. Until GHO-15 every real submission
# was followed by a flat 30-45 s sleep: 8% of the apply stage on 2026-10-11/14,
# and in a parallel run each form worker slept after every submit even though
# its next job was a different employer on a different ATS. The spacing only
# matters between two applications to the same site, so it is now enforced
# there: a job starts no sooner than the delay after this worker's last
# submission to the same host. LinkedIn (every Easy Apply job) stays paced
# exactly as before.
_next_start_by_host: dict[str, float] = {}


def _pace_host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _note_submission(url: str, *, now: float | None = None) -> None:
    delay = constants.apply_delay_seconds() + random.randint(0, constants.delay_jitter_seconds())
    if delay > 0:
        _next_start_by_host[_pace_host(url)] = (time.monotonic() if now is None else now) + delay


def _pacing_wait(url: str, *, now: float | None = None) -> float:
    """Seconds to wait before starting a job on `url`'s host (0 = go now)."""
    ready = _next_start_by_host.get(_pace_host(url))
    if ready is None:
        return 0.0
    return max(0.0, ready - (time.monotonic() if now is None else now))


_SIGNIN_WORD_RE = re.compile(r"sign[- ]?in|log[- ]?in|google", re.I)


def _google_form_wall(outcome, cdp: str) -> bool:
    """The agent stopped at a Google Form that needs a Google sign-in, and the
    apply browser is NOT signed in to Google, so parking is right.

    If the browser IS signed in and the form still refuses (for example a form
    limited to one company's Workspace domain), this returns False and the
    failure is charged like any other, so the job cannot loop forever.
    """
    if outcome.status != "stuck":
        return False
    if blockers.permanent_reason(outcome.status, outcome.detail):
        return False  # e.g. a Workspace-only form: no account of ours opens it
    detail = outcome.detail or ""
    if not google_forms.is_signin_wall(detail):
        # A vaguer DETAIL ("sign-in wall") still counts if the browser is
        # sitting on Google's sign-in page for a form. Only look when the
        # DETAIL is about signing in, so ordinary failures cost no CDP call.
        if not _SIGNIN_WORD_RE.search(detail):
            return False
        if not google_forms.is_signin_wall("", google_forms.page_urls(cdp)):
            return False
    return google_forms.browser_signed_in(cdp) is not True


def _is_usage_limit(detail: str | None) -> bool:
    """The model account is out of quota.

    Only for runs that died WITHOUT a verdict (the harness's own error text):
    an agent-written DETAIL can legitimately say an ATS "rate limited" us,
    and that must stay a site failure, not stop the whole stage.
    """
    d = (detail or "").lower()
    return d.startswith("agent produced no") and any(m in d for m in _USAGE_LIMIT_MARKERS)


def _is_infra_failure(detail: str | None) -> bool:
    """Is this OUR browser breaking, rather than the job being impossible?

    Literal markers alone lose this game. The agent writes the detail string
    freely, so the same fault arrives worded differently every run - on
    2026-09-13 job611 produced "Browser/CDP session unresponsive" one run after
    "Browser session unresponsive at CDP layer", and the slash alone defeated a
    freshly-added literal. Each miss charges a real attempt for something that
    was never the job's fault.

    So match the SHAPE: a browser/CDP subject next to a broken-transport verb.
    Both halves are required, which keeps genuine site failures out - a posting
    that is merely slow "in the browser" has no broken-transport verb, and a
    form that "crashed" has no browser subject.
    """
    d = (detail or "").lower()
    if any(marker in d for marker in _INFRA_FAILURE_MARKERS) or _is_usage_limit(d):
        return True
    return bool(_INFRA_SHAPE_RE.search(d))


# Subject (our browser layer) + verb (the transport is broken), in either
# order and tolerant of the punctuation and filler the agent inserts between
# them. Deliberately narrow on the verb side: "slow", "confusing" or "empty"
# are not infrastructure failures.
_INFRA_SHAPE_RE = re.compile(
    r"(?:browser|chromium|chrome|cdp|devtools)[\w/\s\-()]{0,40}?"
    r"(?:unresponsive|not responding|unreachable|refused|disconnect\w*|"
    r"crash\w*|died|dead|lost connection|timing out|timed out repeatedly)"
    r"|(?:unresponsive|unreachable|refused|disconnect\w*|crash\w*|died|dead)"
    r"[\w/\s\-()]{0,40}?(?:browser|chromium|chrome|cdp|devtools)",
    re.I,
)


@dataclass
class Module4Result:
    attempted: int = 0
    submitted: int = 0
    closed: int = 0
    already_applied: int = 0
    failed: int = 0
    dropped: int = 0
    circuit_broken: int = 0
    preflight_closed: int = 0
    infra_failures: int = 0
    blocked: int = 0
    # Dropped without another attempt: the failure is one a retry cannot fix
    # (module4/blockers.py). Also counted in `dropped`.
    permanent_blocked: int = 0
    # Parked on a temporarily paused ATS (policy/paused_sites.py); no attempt charged.
    paused: int = 0
    # Google Form behind Google sign-in: kept in the queue, no attempt
    # charged, skipped without an agent until the browser is signed in.
    google_signin_parked: int = 0
    # hCaptcha wall on an Oracle Cloud / iCIMS sign-in gate or a Paycom /
    # Lever site (module4/captcha_walls.py):
    # kept in the queue, no attempt charged, skipped by later runs.
    captcha_parked: int = 0
    # Mailto-only postings (module4/email_apply.py): applications sent by
    # email (also counted in `submitted`), and drafts parked for the
    # candidate to read first (no attempt charged).
    emailed: int = 0
    email_drafted: int = 0
    # The apply browser lost its LinkedIn session; the stage stopped so the
    # rest of the queue keeps its attempts (module4/linkedin_session.py).
    linkedin_signed_out: bool = False
    linkedin_signed_out_reason: str = ""
    # The weekly Claude budget hit the stop threshold (module4/budget_guard.py)
    # between jobs; the stage stopped cleanly with the in-flight job's own
    # result already recorded, no attempt charged to anything after it.
    budget_exhausted: bool = False
    budget_utilization: float | None = None
    # The 5-hour session window (GHO-22): times claims paused until it reset,
    # and why the stage stopped on it when the reset fell past the deadline.
    session_pauses: int = 0
    session_limit_stop: str = ""
    # The Hermes the agent would run executes turns in its shared gateway,
    # which cannot see the agent's CDP browser (agent_driver.hermes_gateway_only);
    # the stage did not start, nothing was charged.
    hermes_unusable: str = ""
    # Several agents in a row looked like they drive some other browser
    # (MAX_ATTACH_MISMATCHES, which puts the streak's jobs back as they were;
    # MAX_CONSECUTIVE_SIGN_IN_MISREADS as a backstop); the stage stopped.
    agent_browser_mismatch: str = ""
    # Parallel runs (module4/parallel.py): listings the LinkedIn worker routed
    # to a form worker / kept for itself, and jobs a form worker handed back.
    routed_form: int = 0
    routed_linkedin: int = 0
    rerouted: int = 0
    # The attach canary (agent_driver.attach_canary) showed the agent's
    # browser tools are not on this worker's browser: nothing was claimed.
    attach_failed: str = ""
    details: list[str] = field(default_factory=list)

    @property
    def real_submissions(self) -> int:
        """Applications genuinely sent this run.

        Deliberately excludes `closed` / `already_applied`: those mark a job
        done in the DB but sent nothing. Conflating them is exactly the bug
        that made the 2026-09-01 summary claim 44 submissions when 13 went out.
        """
        return self.submitted


@dataclass
class HealthVerdict:
    """Whether a run's OUTCOME was healthy, not merely whether it ran.

    The pipeline used to report `run complete, failed=0` whenever each stage's
    process exited 0 - which it does even when every single job inside it
    failed. The 2026-09-03 run sent 9 of 38 and still reported a clean run.
    """

    healthy: bool
    label: str
    reasons: list[str] = field(default_factory=list)


# A run where most real attempts failed is a broken run, even if the process
# exited cleanly. Tuned to catch "the machinery is broken" without firing on
# an ordinary run where a few hard ATS portals beat us.
UNHEALTHY_FAILURE_RATIO = 0.5


def health_verdict(result: "Module4Result") -> HealthVerdict:
    """Classify a finished apply stage as healthy or not.

    Only counts things that indicate OUR machinery misbehaving:
      * a high share of genuine failures among real attempts
      * any infra failure (browser died) - always worth surfacing
    Explicitly NOT unhealthy: dead listings, already-applied jobs, blocked
    employers, and preflight skips. Those are correct outcomes, and an
    all-blocked or all-dead batch must not look like a malfunction.
    """
    reasons: list[str] = []

    # Real attempts = jobs where we actually tried to fill a form.
    real_attempts = result.submitted + result.failed
    if real_attempts:
        ratio = result.failed / real_attempts
        if ratio >= UNHEALTHY_FAILURE_RATIO:
            reasons.append(
                f"{result.failed}/{real_attempts} real attempts failed "
                f"({ratio:.0%}) - check the failure details above, not just "
                f"that the stage 'completed'"
            )
    if result.infra_failures:
        reasons.append(
            f"{result.infra_failures} infrastructure failure(s) - our browser "
            f"broke mid-run; these jobs were NOT charged an attempt"
        )
    if result.circuit_broken:
        reasons.append(
            f"{result.circuit_broken} job(s) dropped by the domain circuit breaker"
        )
    if result.linkedin_signed_out:
        reasons.append(
            "LinkedIn is SIGNED OUT in the apply browser - the stage stopped early and "
            "no job was charged for it. the user must sign in again (check with "
            "`.venv/bin/python scripts/linkedin_session_check.py`) before the next run"
            + (f" ({result.linkedin_signed_out_reason})" if result.linkedin_signed_out_reason else "")
        )
    if result.hermes_unusable:
        reasons.append(result.hermes_unusable)
    if result.agent_browser_mismatch:
        reasons.append(result.agent_browser_mismatch)
    if result.session_limit_stop:
        reasons.append(f"stopped on the Claude 5-hour session limit: {result.session_limit_stop}")

    if result.attach_failed:
        # Not a degraded run: no apply agent could have worked at all.
        return HealthVerdict(False, "FAILED", [result.attach_failed, *reasons])

    if not reasons:
        return HealthVerdict(True, "OK", [])
    return HealthVerdict(False, "DEGRADED", reasons)


def hermes_preflight(result: "Module4Result") -> bool:
    """Refuse to start when the agent's Hermes runs turns in its shared gateway.

    Such a Hermes drives a fresh local browser, never the CDP browser we hand
    it, so every job would fail (and be charged) without a form ever being
    seen (GHO-13, 15 Oct 2026). Returns False and records why when unusable.
    """
    try:
        binary = agent_driver._hermes_bin()
    except RuntimeError as e:
        binary, unusable = "", str(e)
    else:
        unusable = "" if not agent_driver.hermes_gateway_only(binary) else (
            f"Hermes at {binary} runs every agent turn in its shared gateway, which never sees "
            "the agent's BROWSER_CDP_URL - agents would drive a blank local browser. The apply "
            "stage did not start and no job was charged. Pin a working Hermes with "
            "scripts/pin_hermes.sh (GHO-13)"
        )
    if not unusable:
        return True
    logger.error("Apply stage not started: %s", unusable)
    result.hermes_unusable = unusable
    result.details.append(f"ABORTED: {unusable[:200]}")
    return False


def _attach_canary_check(attach_check, cdp: str, worker) -> agent_driver.AttachCheck | None:
    """Run the attach canary for this worker; None when it is switched off."""
    if attach_check is None:
        if not agent_driver.canary_enabled():
            return None
        attach_check = agent_driver.attach_canary
    name = worker.name if worker is not None else "main"
    logger.info("Attach canary (%s): checking the apply agent drives %s ...", name, cdp)
    try:
        return attach_check(cdp, name)
    except Exception as e:
        return agent_driver.AttachCheck(False, f"attach canary raised: {e}")


def _attach_mismatch_reason(outcome, signout_confirmed) -> str:
    """Why this outcome looks like an agent on some other browser, or ''."""
    if outcome.status == "submitted":
        return ""
    if signout_confirmed is not None and signout_confirmed.signed_in:
        return f"agent saw a LinkedIn sign-in wall, the feed probe says signed in ({signout_confirmed.reason[:80]})"
    if getattr(outcome, "upload_no_file_input", 0) and not getattr(outcome, "resume_uploads_ok", 0):
        return "upload found no <input type=file> in any open tab"
    return ""


_JOB_STATE_FIELDS = (
    "applied", "should_apply", "module4_attempts", "module4_last_status",
    "module4_closed_at", "module4_submitted_at", "module4_outcome",
)


def _counters(result: "Module4Result") -> dict:
    return {k: v for k, v in vars(result).items() if isinstance(v, int) and not isinstance(v, bool)}


def _counts_since(result: "Module4Result", before: dict) -> dict:
    return {k: v - before.get(k, 0) for k, v in _counters(result).items()}


def _trip_attach_breaker(session, result, streak, reasons, worker, *, dry_run) -> None:
    """Put every job of the streak back exactly as it was before it ran, so
    they stay re-claimable with no attempt charged, take its counts back out
    of the result, and record the stop. `streak` holds (job, DB state before
    its agent ran, what it added to the counters)."""
    for job, before, counted in streak:
        for k, v in before.items():
            setattr(job, k, v)
        if not dry_run:
            session.add(job)
        for k, v in counted.items():
            setattr(result, k, getattr(result, k) - v)
    if not dry_run:
        session.commit()
    ids = [job.id for job, _, _ in streak]
    name = worker.name if worker is not None else "main"
    result.agent_browser_mismatch = (
        f"worker {name} stopped the run: {len(ids)} jobs in a row looked like the apply agent is not "
        f"driving this browser ({'; '.join(dict.fromkeys(reasons))}). Jobs {', '.join(map(str, ids))} "
        "were put back untouched, no attempt charged (GHO-17)"
    )
    logger.error("ATTACH MISMATCH: %s", result.agent_browser_mismatch)
    result.details.append(f"ABORTED: {result.agent_browser_mismatch}")
    # Every worker's agent runs the same Hermes, so the others are no better off.
    if worker is not None:
        worker.stop_all("agents do not see the worker's browser")


def _stop_for_linkedin_sign_out(result: "Module4Result", jid, reason: str, left: int) -> None:
    """Record a LinkedIn sign-out and the clean stop that follows it."""
    result.linkedin_signed_out = True
    result.linkedin_signed_out_reason = reason[:160]
    logger.error(
        "[%s] HEALTH: LinkedIn is SIGNED OUT in the apply browser (%s). Stopping the "
        "apply stage with %d job(s) untouched so they keep their attempts. the user must sign "
        "in to LinkedIn again before the next run.",
        jid,
        reason[:160],
        left,
    )
    result.details.append(
        f"ABORTED: LinkedIn signed out ({reason[:120]}); {left} job(s) left untouched, "
        "no attempt charged"
    )


def _pause_for_session_reset(resets_at, cause, deadline, result, worker, left) -> bool:
    """Hold claims until the 5-hour window resets. False = the stage stopped.

    Pauses only when the reset leaves real working time before the stage
    deadline; otherwise (or with no known reset time) stops cleanly, which
    beats starting agents that die mid-form on the provider's 429.
    """
    remaining = left()
    pause_ok = budget_guard.session_pause_enabled()
    wait = budget_guard.session_pause_seconds(
        resets_at, now=_wall(), seconds_to_deadline=deadline - _monotonic()
    ) if pause_ok else None
    if wait is None:
        if not pause_ok:
            reason = (
                f"{cause}; the user's rule is to stop at this line, not wait for the reset "
                f"({resets_at or 'unknown'}), so it stopped with {remaining} job(s) left queued"
            )
        else:
            reason = (
                f"{cause}; the window resets {resets_at or 'at an unknown time'}, too late "
                f"for this stage's deadline, so it stopped with {remaining} job(s) left queued"
            )
        logger.error("Stopping the apply stage: %s.", reason)
        result.session_limit_stop = reason
        result.details.append(f"SESSION STOP: {reason}")
        if worker is not None:
            worker.stop_all("5h session limit")
        return False
    logger.warning(
        "%s: pausing claims for %.0f min until the 5h window resets (%s); %d job(s) wait.",
        cause, wait / 60, resets_at, remaining,
    )
    result.session_pauses += 1
    result.details.append(f"SESSION PAUSE: {cause}; paused {wait / 60:.0f} min until {resets_at}")
    end = _monotonic() + wait
    while (rest := end - _monotonic()) > 0:
        stop = worker.queue.stop_reason() if worker is not None else None
        if stop:
            logger.warning("Run stop flag set during the session pause (%s); stopping.", stop)
            result.details.append(f"session pause ended by the run stop flag ({stop})")
            return False
        _sleep(min(rest, SESSION_PAUSE_POLL_SEC))
    logger.info("5h session pause over; resuming claims.")
    return True


def _apply_by_email(session, job, outcome, result: "Module4Result", pace_url: str) -> None:
    """Send (or draft) the application email a mailto-only posting asks for."""
    jid = job.id
    mailto = email_apply.parse_mailto(outcome.email_mailto or outcome.detail)
    try:
        sent = email_apply.apply_by_email(job, mailto, outcome.detail)
    except Exception as e:  # noqa: BLE001 - any failure is an ordinary failed attempt
        logger.warning("[%s] Email application failed: %s", jid, e)
        failed = agent_driver.ApplyOutcome("stuck", f"email application to {mailto.to} failed: {e}")
        persist.record_failed_attempt(session, job, failed)
        result.failed += 1
        result.details.append(f"job_id={jid}: EMAIL FAILED ({e})")
        return
    if sent.sent:
        persist.mark_applied(session, job, status=sent.status)
        result.submitted += 1
        result.emailed += 1
        result.details.append(f"job_id={jid}: SUBMITTED by email to {sent.to} ({sent.message_id})")
        _note_submission(pace_url)
    else:
        persist.mark_status(session, job, sent.status)
        result.email_drafted += 1
        result.details.append(f"job_id={jid}: EMAIL DRAFTED to {sent.to} ({sent.draft_id})")


def _send_parked_draft(session, job, parked: tuple[str, str], result: "Module4Result") -> None:
    jid = job.id
    draft_id, to = parked
    try:
        mid = email_apply.send_draft(draft_id)
    except Exception as e:  # noqa: BLE001 - leave it parked; the next run tries again
        logger.warning("[%s] Sending parked email draft %s failed: %s", jid, draft_id, e)
        result.failed += 1
        result.details.append(f"job_id={jid}: EMAIL DRAFT SEND FAILED ({e})")
        return
    persist.mark_applied(session, job, status=email_apply.sent_status(mid, to))
    result.submitted += 1
    result.emailed += 1
    result.details.append(f"job_id={jid}: SUBMITTED by email to {to} ({mid})")
    logger.info("[%s] SUBMITTED: parked email draft %s sent (gmail id %s).", jid, draft_id, mid)


def run_module4(
    session: Session,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    job_id: int | None = None,
    budget_check: Callable[[], budget_guard.BudgetCheck] | None = None,
    worker=None,
    attach_check: Callable[[str, str], agent_driver.AttachCheck] | None = None,
    canary_only: bool = False,
) -> Module4Result:
    """`budget_check` defaults to the real weekly-usage read (module4/budget_guard.py).

    Tests that are not exercising the budget guard itself inject a stub here
    instead of letting every job iteration hit the real Anthropic usage
    endpoint - that network call is exactly what the isolated regression
    harness's fixture-only network guard exists to refuse.

    `worker` (module4.parallel.Worker) makes this one worker of a parallel
    run: jobs are claimed one at a time from the run's shared queue instead
    of listed up front, and the LinkedIn worker routes listings to form
    workers. None is the single-worker runner, unchanged.

    `attach_check(cdp_url, worker_name)` defaults to agent_driver.attach_canary
    (off when JOBO_ATTACH_CANARY=0); `canary_only` stops right after it.
    """
    budget_check = budget_check or (
        lambda: budget_guard.check_budget(
            threshold=budget_guard.threshold_from_env(),
            override=budget_guard.override_from_env(),
        )
    )
    result = Module4Result()
    if not dry_run and not hermes_preflight(result):
        if worker is not None:
            worker.stop_all("apply agent's Hermes is gateway-only")
        return result
    if worker is not None:
        jobs = worker.jobs(session)

        def left() -> int:
            return worker.remaining()

        logger.info("Module 4 worker %s (%s): claiming jobs from the run queue.", worker.name, worker.role)
    else:
        jobs = query.list_jobs_to_apply(session, limit=limit, force=force, job_id=job_id)
        cap = constants.max_jobs_per_run()
        if cap and not limit:
            jobs = jobs[:cap]
        if not jobs and not canary_only:
            logger.info("No jobs eligible for submission.")
            return result

        def left() -> int:
            return len(jobs) - result.attempted

        logger.info("Module 4: %s jobs queued for submission.", len(jobs))
    kept: list[str] = []  # CDP URLs this call keeps frames on
    try:
        _apply_loop(session, jobs, result, left, dry_run=dry_run, force=force,
                    budget_check=budget_check, worker=worker,
                    attach_check=attach_check, canary_only=canary_only, kept=kept)
    finally:
        for url in kept:
            frame_keeper.stop(url)
        if worker is not None:
            jobs.close()  # hands an untouched claim back to the queue
    return result


def _apply_loop(session, jobs, result, left, *, dry_run, force, budget_check, worker,
                attach_check=None, canary_only=False, kept=None) -> None:
    kept = [] if kept is None else kept
    try:
        cdp = browser_session.ensure_browser_running(constants.browser_profile_dir())
    except RuntimeError as e:
        logger.error("Could not start/attach browser for apply stage: %s", e)
        result.details.append(f"ABORTED: browser startup failed: {e}")
        return
    # Overnight the headed browser draws no frames, and Workday re-enables
    # Save and Continue from an animation frame (GHO-72, module4/frame_keeper.py).
    if frame_keeper.ensure(cdp):
        kept.append(cdp)

    # Before the first claim: prove the agent drives this worker's browser.
    canary = _attach_canary_check(attach_check, cdp, worker)
    if canary is not None and not canary.ok:
        name = worker.name if worker is not None else "main"
        result.attach_failed = (
            f"attach canary FAILED on worker {name} (CDP {cdp}): {canary.detail[:300]}. "
            "The apply stage stopped before claiming any job; nothing was charged (GHO-17)"
        )
        logger.error("ATTACH CANARY FAILED: %s", result.attach_failed)
        result.details.append(f"ABORTED: {result.attach_failed}")
        if worker is not None:
            worker.stop_all("attach canary failed")
        return
    if canary is not None:
        logger.info("Attach canary PASSED on CDP port %s: %s", cdp.rsplit(":", 1)[-1], canary.detail)
    if canary_only:
        return

    # Jobs in a row that look like an agent on another browser (GHO-17):
    # (job, its DB state before the agent ran, what it added to the
    # counters) and why each counted. The last one's counts are only known
    # once its accounting is done, at the top of the next iteration.
    streak: list[tuple[JobModel, dict, dict]] = []
    streak_reasons: list[str] = []
    unsettled: tuple[JobModel, dict, dict] | None = None

    deadline = _monotonic() + constants.stage_deadline_seconds()
    deadline_hit = False
    consecutive_infra = 0
    consecutive_misreads = 0
    walled = captcha_walls.walled_job_hosts(
        session.query(JobModel).filter(JobModel.module4_last_status.like(f"{captcha_walls.STATUS_PREFIX}%"))
    )

    for job in jobs:
        if unsettled is not None:
            streak.append((unsettled[0], unsettled[1], _counts_since(result, unsettled[2])))
            unsettled = None
        if _monotonic() >= deadline:
            remaining = left()
            logger.warning(
                "Apply stage deadline reached (%.0fs). Stopping cleanly; "
                "%d job(s) remain queued for the next slot.",
                constants.stage_deadline_seconds(),
                remaining,
            )
            result.details.append(
                f"deadline reached with {remaining} job(s) left queued"
            )
            deadline_hit = True
            break

        # Weekly Claude budget guard: checked BETWEEN jobs (every iteration),
        # not only at stage start, so a budget that crosses the threshold
        # mid-run stops the stage before the NEXT job starts rather than
        # after the whole queue. The job already in flight (this iteration)
        # already finished by the time this check runs again, so nothing is
        # interrupted mid-application.
        budget = budget_check()
        logger.info("Claude usage before job %s: %s", job.id, budget_guard.usage_summary(budget))
        if budget.should_stop:
            remaining = left()
            if worker is not None:
                worker.stop_all("weekly budget threshold")
            logger.warning(
                "Weekly Claude budget at %.0f%% (>= %.0f%% threshold). Stopping the "
                "apply stage cleanly; %d job(s) remain queued for the next slot.",
                budget.utilization * 100,
                budget.threshold * 100,
                remaining,
            )
            budget_guard.write_resume_marker(
                budget_guard.RESUME_MARKER_PATH,
                check=budget,
                stage="apply",
                jobs_done=result.attempted,
                jobs_remaining=remaining,
            )
            result.budget_exhausted = True
            result.budget_utilization = budget.utilization
            result.details.append(
                f"BUDGET STOP: weekly utilization {budget.utilization:.0%} >= "
                f"{budget.threshold:.0%} threshold; {remaining} job(s) left queued, "
                f"marker written to {budget_guard.RESUME_MARKER_PATH}"
            )
            break

        # The 5-hour session window (GHO-22): start no agent into it. Agents
        # it cuts off die mid-form and can leave a half-made ATS account.
        session_ok = True
        while budget.session_limited:
            cause = (
                f"5h session usage {budget.session_utilization:.0%} >= "
                f"{budget.session_threshold:.0%} threshold"
            )
            if not _pause_for_session_reset(budget.session_resets_at, cause, deadline, result, worker, left):
                session_ok = False
                break
            budget = budget_check()
            logger.info("Claude usage after the pause: %s", budget_guard.usage_summary(budget))
        if not session_ok:
            break

        jid = job.id

        # Hard employer blocklist: a last-resort gate. query.list_jobs_to_apply
        # already filters these out, but this check is deliberately duplicated
        # so no future caller (a new script, a --force path, a hand-built job
        # list) can route a blocked employer to a live browser. Costs one
        # string comparison per job and does NOT touch attempts or should_apply
        # - a blocked job is not a failed job, it is a job we refuse.
        blocked_reason = employer_blocklist.job_block_reason(job)
        if blocked_reason:
            result.blocked += 1
            result.details.append(f"job_id={jid}: BLOCKED EMPLOYER ({blocked_reason})")
            logger.warning("[%s] Refusing, blocked employer: %s", jid, blocked_reason)
            continue

        # A Gmail draft parked by draft mode (module4/email_apply.py): sent as
        # it stands once the mode is "send", with no agent; else left waiting.
        parked = email_apply.parked_draft(job.module4_last_status)
        if parked:
            if email_apply.mode() != "send" or dry_run:
                result.email_drafted += 1
                result.details.append(f"job_id={jid}: EMAIL DRAFT waiting ({parked[0]})")
                continue
            _send_parked_draft(session, job, parked, result)
            continue

        # Circuit breaker: skip jobs on domains with repeated hard failures
        # without spawning an agent at all.
        tripped_reason = circuit_breaker.is_tripped(job.job_url)
        if tripped_reason:
            outcome = agent_driver.ApplyOutcome(status="stuck", detail=tripped_reason)
            attempts = persist.record_failed_attempt(session, job, outcome, dry_run=dry_run)
            persist.drop_permanently(session, job, dry_run=dry_run)
            result.circuit_broken += 1
            result.failed += 1
            result.details.append(f"job_id={jid}: CIRCUIT-BROKEN, dropped ({tripped_reason})")
            logger.warning("[%s] Skipped (circuit breaker): %s", jid, tripped_reason)
            continue

        # A Google Form behind Google sign-in (module4/google_forms.py). Kept
        # in the queue but skipped without an agent until the apply browser
        # is signed in to Google or the form opens signed out. Also catches
        # older rows whose stored DETAIL describes the same wall.
        if not force and (
            google_forms.is_parked(job.module4_last_status)
            or (
                (job.module4_attempts or 0) > 0
                and google_forms.is_signin_wall(job.module4_last_status)
            )
        ):
            release = google_forms.release_reason(job.module4_last_status, cdp)
            if release is None:
                if not google_forms.is_parked(job.module4_last_status):
                    last = job.module4_last_status or ""
                    persist.mark_status(
                        session,
                        job,
                        google_forms.status_for(
                            google_forms.find_form_url(last), last.split(":", 1)[-1].strip()
                        ),
                        dry_run=dry_run,
                    )
                result.google_signin_parked += 1
                result.details.append(
                    f"job_id={jid}: PARKED, Google Form needs a signed-in Google account"
                )
                logger.info("[%s] Parked: Google Form needs Google sign-in.", jid)
                continue
            logger.info("[%s] Google Form job released: %s.", jid, release)

        # A job whose last failure was a wall another attempt cannot get past
        # (captcha, required DOB/PAN, Google-only sign-in, dead account) is
        # dropped here without spawning an agent. 35% of all apply tokens to
        # 2026-09-26 went to repeat attempts at jobs that never got submitted.
        if (job.module4_attempts or 0) > 0 and not force:
            last_status, _, last_detail = (job.module4_last_status or "").partition(":")
            gone = blockers.dead_listing_reason(last_status, last_detail, job.job_url)
            if gone:
                persist.mark_closed(session, job, status="closed", detail=f"{gone}: {last_detail}",
                                    dry_run=dry_run)
                result.closed += 1
                result.details.append(f"job_id={jid}: closed, not retried ({gone})")
                logger.info("[%s] Not retrying, listing is gone: %s.", jid, gone)
                continue
            perm = blockers.permanent_reason_from_last_status(job.module4_last_status)
            if perm:
                persist.drop_permanently(session, job, dry_run=dry_run)
                result.dropped += 1
                result.permanent_blocked += 1
                result.details.append(f"job_id={jid}: DROPPED, not retryable ({perm})")
                logger.info("[%s] Not retrying: %s.", jid, perm)
                continue

        # A job_url already on a paused ATS never reaches an agent.
        paused_site = paused_sites.paused_site_for_url(str(job.job_url or ""))
        if paused_site:
            persist.mark_status(
                session, job, paused_sites.status_for(paused_site, "job URL is on a paused site"),
                dry_run=dry_run,
            )
            result.paused += 1
            result.details.append(f"job_id={jid}: PAUSED SITE ({paused_site})")
            continue

        # Same for a job_url on a host where another job is parked behind an
        # hCaptcha sign-in wall (GHO-54: it meets the same wall), or on a
        # site-wall host that challenges every visitor (GHO-73: Paycom, Lever).
        wall_host = captcha_walls.walled_host_for_url(job.job_url, walled)
        if wall_host and not force:
            persist.mark_status(
                session, job,
                captcha_walls.status_for(wall_host, captcha_walls.pre_park_detail(wall_host)),
                dry_run=dry_run,
            )
            result.captcha_parked += 1
            result.details.append(f"job_id={jid}: PARKED, hCaptcha sign-in wall on {wall_host}")
            logger.info("[%s] Parked without an agent: hCaptcha sign-in wall on %s.", jid, wall_host)
            continue

        counts_before_job = _counters(result)
        result.attempted += 1

        # The browser must be verified BEFORE EVERY JOB. It used to be started
        # once per run, so anything that killed Chromium mid-run turned every
        # remaining job into an instant failure - on the 2026-09-03 run the
        # browser died ~20min in and 36 consecutive jobs failed in ~10s each
        # against a dead CDP endpoint, burning a real attempt on all of them.
        healthy_cdp = browser_session.ensure_healthy(constants.browser_profile_dir())
        # Clear leftover tabs BEFORE the job, not only after it. The trailing
        # cleanup never runs before the FIRST job, so tabs left by a previous
        # `jobo apply` invocation survive into this one - and duplicate tabs
        # are not cosmetic: the uploader scans every target, so a stale copy of
        # an ATS form contributes an input identical to the live one and the
        # resume can be attached to the tab nobody is looking at (Westbay /
        # Rippling, 2026-09-11: 2 live inputs, 8 candidates, uploads "succeeded"
        # into a background tab twice before landing).
        if healthy_cdp:
            browser_session.close_extra_tabs()
        if not healthy_cdp:
            logger.error(
                "[%s] Browser is unavailable and could not be relaunched; "
                "stopping the apply stage so the remaining jobs keep their attempts.",
                jid,
            )
            result.attempted -= 1
            result.details.append(
                "ABORTED: browser unavailable and could not be relaunched; "
                f"{left()} job(s) left untouched"
            )
            break
        cdp = healthy_cdp
        if frame_keeper.ensure(cdp) and cdp not in kept:  # a relaunch may move the URL
            kept.append(cdp)

        # LinkedIn session check BEFORE spawning an agent. Every queued job
        # starts on a LinkedIn listing, so a signed-out browser fails them all
        # ("LinkedIn requires sign-in") - on 2026-10-03 that drained the last
        # attempt of 1140, 1173 and 1220 in a row. One read-only CDP call
        # (cookie NAMES only, see module4/linkedin_session.py) answers it.
        # A direct employer link (jobfound, module4/routing.py) never opens
        # LinkedIn, so a signed-out browser is no reason to stop on it; the
        # next LinkedIn listing still gets the check.
        if linkedin_session.enabled() and routing.is_linkedin_listing(job.job_url):
            session_state = linkedin_session.check_session(cdp)
            if session_state.signed_out:
                result.attempted -= 1
                _stop_for_linkedin_sign_out(result, jid, session_state.reason, left())
                break
            if session_state.state == "unknown":
                logger.warning("[%s] LinkedIn session check inconclusive: %s", jid, session_state.reason)

        # Cheap dead-listing preflight BEFORE spawning an agent. On the
        # 2026-09-01 run, 31 of 63 attempted jobs (49%) turned out to be
        # closed LinkedIn postings, each costing a full ~2min agent spawn to
        # discover. One HTTP GET answers the same question in ~1s.
        # Deliberately conservative: only LinkedIn URLs, only an exact known
        # phrase, and anything ambiguous falls through to the agent (see
        # module4/preflight.py), so this can never cause a missed application.
        pf = (
            preflight.PreflightResult(closed=False, reason="preflighted when routed")
            if worker is not None and worker.already_preflighted
            else preflight.check_job_open(str(job.job_url or ""))
        )
        if pf.should_skip:
            persist.mark_closed(
                session, job, status="closed", detail=pf.reason, dry_run=dry_run
            )
            result.preflight_closed += 1
            result.closed += 1
            result.details.append(f"job_id={jid}: closed via preflight ({pf.reason})")
            logger.info("[%s] Preflight: listing is closed (%s). Skipping.", jid, pf.reason)
            continue

        # Parallel run, LinkedIn worker: open the listing (no model) and route
        # it. Nothing is applied here, so nothing is charged.
        if worker is not None and worker.routing:
            route = linkedin_router.resolve(cdp, str(job.job_url or ""))
            logger.info("[%s] Routed: %s %s (%s)", jid, route.kind, route.apply_url[:100], route.reason)
            if route.kind == linkedin_router.CLOSED:
                persist.mark_closed(session, job, status="closed", detail=route.reason, dry_run=dry_run)
                result.closed += 1
                result.details.append(f"job_id={jid}: closed when routed ({route.reason})")
                continue
            result.attempted -= 1
            site_wall = (
                captcha_walls.site_wall_host_for_url(route.apply_url)
                if route.kind == linkedin_router.EXTERNAL and not force else None
            )
            if site_wall:
                # Applies on a site that challenges every visitor (GHO-73):
                # park it here instead of handing it to a form agent.
                persist.mark_status(
                    session, job,
                    captcha_walls.status_for(site_wall, captcha_walls.pre_park_detail(site_wall)),
                    dry_run=dry_run,
                )
                result.captcha_parked += 1
                result.details.append(f"job_id={jid}: PARKED when routed, hCaptcha wall on {site_wall}")
                logger.info("[%s] Parked when routed (no attempt charged): hCaptcha wall on %s.", jid, site_wall)
            elif route.kind == linkedin_router.EXTERNAL and google_forms.find_form_url(route.apply_url) is None:
                worker.hand_off(jid, "form", route.apply_url, f"external: {route.reason}")
                result.routed_form += 1
            else:
                # Easy Apply, a Google Form (the signed-in browser may hold
                # Google too) or unroutable: applied here, as before.
                worker.hand_off(jid, "linkedin", route.apply_url or None, f"{route.kind}: {route.reason}")
                result.routed_linkedin += 1
            browser_session.close_extra_tabs()
            continue

        resume = (job.module3_resume_pdf_path or "").strip()
        cover = (job.module3_cover_pdf_path or "").strip()
        if not resume or not Path(resume).is_file():
            persist.mark_status(
                session, job, "failed:missing_resume", dry_run=dry_run, count_as_attempt=True
            )
            if job.module4_attempts >= constants.max_attempts_per_job():
                persist.drop_permanently(session, job, dry_run=dry_run)
                result.details.append(
                    f"job_id={jid}: DROPPED after {job.module4_attempts} attempts (resume never materialized)"
                )
            result.failed += 1
            result.details.append(f"job_id={jid}: resume missing ({resume})")
            continue

        is_retry = (job.module4_attempts or 0) > 0
        previous_failure = None
        budget_boost = False
        last_was_sign_out = linkedin_session.is_signed_out_detail(job.module4_last_status)
        if is_retry and job.module4_last_status and not last_was_sign_out:
            # Strip the leading "stuck:"/"captcha_failed:" status prefix, keep
            # just the reason text, so the retry prompt can steer around it.
            previous_failure = job.module4_last_status.split(":", 1)[-1].strip() or None
            # If the previous attempt died from running out of turns or time,
            # halving the budget (the old retry behaviour) guaranteed the same
            # ending. Those jobs were usually 90%+ through the form. Give them
            # the full first-attempt budget instead.
            budget_boost = _is_budget_exhaustion(str(job.module4_last_status or ""))

        # The agent uploads a byte-identical copy named for this job
        # (<First>_<Last>_Resume_<id>_<hash>.pdf), so the file it sends can be
        # told apart from resumes a site already holds (TODO item 18).
        upload_resume = upload_names.prepare(jid, str(Path(resume).resolve()), "resume")
        upload_cover = (upload_names.prepare(jid, str(Path(cover).resolve()), "cover")
                        if cover and Path(cover).is_file() else None)
        form_url = worker.apply_url if worker is not None else None
        pace_url = str(form_url or job.job_url or "")
        wait = _pacing_wait(pace_url)
        if wait > 0:
            logger.info("Sleeping %ss (post-submission pacing, %s)...", round(wait), _pace_host(pace_url))
            time.sleep(wait)
        user_prompt = prompts.user_prompt(
            job_id=jid,
            company=job.company_name or "",
            title=job.job_title or "",
            url=form_url or job.job_url,
            listing_url=job.job_url if form_url else None,
            resume_path=upload_resume or str(Path(resume).resolve()),
            cover_path=upload_cover,
            previous_failure=previous_failure,
            step_budget=agent_driver.step_budget(is_retry=is_retry, budget_boost=budget_boost),
        )
        logger.info(
            "[%s] Applying (attempt %d/%d, %s%s): %s - %s",
            jid,
            job.module4_attempts + 1,
            constants.max_attempts_per_job(),
            "retry" if is_retry else "first try",
            ", full budget" if budget_boost else "",
            job.company_name,
            job.job_title,
        )
        if worker is not None:
            worker.agent_started(jid)
        before_job = {k: getattr(job, k) for k in _JOB_STATE_FIELDS}
        try:
            outcome = agent_driver.run_apply_agent(
                system_prompt=prompts.SYSTEM_PROMPT,
                user_prompt=user_prompt,
                cdp_url=cdp,
                job_id=jid,
                is_retry=is_retry,
                resume_path=upload_resume or str(Path(resume).resolve()),
                cover_path=upload_cover,
                budget_boost=budget_boost,
                source_url=str(form_url or job.job_url or ""),
                company=str(job.company_name or ""),
            )
        except Exception as e:
            outcome = agent_driver.ApplyOutcome(status="stuck", detail=str(e)[:200])

        # The read-only feed probe for an agent-reported LinkedIn sign-out
        # (not on a form worker, whose browser is never signed in).
        signout_confirmed = None
        if (
            outcome.status != "submitted"
            and linkedin_session.is_signed_out_outcome(outcome.status, outcome.detail)
            and not (worker is not None and worker.role == "form")
        ):
            signout_confirmed = linkedin_session.confirm_signed_out(cdp)

        mismatch = _attach_mismatch_reason(outcome, signout_confirmed)
        if not mismatch:
            streak, streak_reasons = [], []
        else:
            streak_reasons.append(mismatch)
            logger.warning("[%s] Attach mismatch %d/%d on this worker: %s",
                           jid, len(streak) + 1, MAX_ATTACH_MISMATCHES, mismatch)
            if len(streak) + 1 >= MAX_ATTACH_MISMATCHES:
                streak.append((job, before_job, _counts_since(result, counts_before_job)))
                _trip_attach_breaker(session, result, streak, streak_reasons, worker, dry_run=dry_run)
                break
            unsettled = (job, before_job, counts_before_job)

        # "LinkedIn requires sign-in" is the browser's state, not the job's.
        # Never charge it; confirm with a read-only probe and stop the stage
        # unless the session is demonstrably fine.
        if (
            worker is not None
            and worker.role == "form"
            and outcome.status != "submitted"
            and linkedin_session.is_signed_out_outcome(outcome.status, outcome.detail)
        ):
            # This browser is never signed in to LinkedIn; the employer's site
            # wanted LinkedIn. Not the job's fault: the signed-in worker takes it.
            if worker.reroute_to_linkedin(jid, f"form worker hit LinkedIn: {outcome.detail[:200]}"):
                result.attempted -= 1
                result.rerouted += 1
                result.details.append(
                    f"job_id={jid}: handed to the LinkedIn worker (site needs LinkedIn; attempt not counted)"
                )
                browser_session.close_extra_tabs()
                continue
        elif signout_confirmed is not None:
            confirmed = signout_confirmed
            if not confirmed.signed_in:
                result.attempted -= 1
                persist.mark_status(
                    session,
                    job,
                    f"{linkedin_session.SIGNED_OUT_STATUS}:{outcome.detail[:500]}",
                    dry_run=dry_run,
                )
                _stop_for_linkedin_sign_out(
                    result, jid, confirmed.reason or outcome.detail[:120], left()
                )
                break
            if not last_was_sign_out:
                # The session checks out, so the agent misread this page. Give
                # it one uncharged retry; a second identical claim on a healthy
                # session falls through and is charged like any other failure.
                logger.warning(
                    "[%s] Agent reported LinkedIn sign-in, but the session is fine (%s); "
                    "not charging this attempt.",
                    jid,
                    confirmed.reason,
                )
                result.attempted -= 1
                persist.mark_status(
                    session,
                    job,
                    f"{linkedin_session.SIGNED_OUT_STATUS}:{outcome.detail[:500]}",
                    dry_run=dry_run,
                )
                result.details.append(
                    f"job_id={jid}: SKIPPED (agent saw a LinkedIn sign-in wall, session "
                    f"verified fine; attempt not counted): {outcome.detail[:100]}"
                )
                consecutive_misreads += 1
                if consecutive_misreads >= MAX_CONSECUTIVE_SIGN_IN_MISREADS:
                    reason = (
                        f"{consecutive_misreads} agents in a row saw a LinkedIn sign-in wall while "
                        "this browser is signed in - the agent is probably not driving this "
                        "browser (check agent.log for 'Resolved CDP endpoint'); stage stopped, "
                        "none of them charged"
                    )
                    logger.error("Stopping the apply stage: %s.", reason)
                    result.agent_browser_mismatch = reason
                    result.details.append(f"ABORTED: {reason}")
                    if worker is not None:
                        worker.stop_all("agents do not see the worker's browser")
                    break
                browser_session.close_extra_tabs()
                continue

        # An infrastructure failure is OUR fault: don't charge the job an
        # attempt, and stop the stage rather than grinding the whole queue
        # against a broken browser (2026-09-03: 36 jobs burned in 10 minutes).
        if outcome.status != "submitted" and _is_infra_failure(outcome.detail):
            logger.error(
                "[%s] Infrastructure failure, not the job's fault: %s",
                jid,
                outcome.detail[:120],
            )
            result.attempted -= 1
            result.infra_failures += 1
            consecutive_infra += 1
            result.details.append(
                f"job_id={jid}: SKIPPED (infrastructure failure, attempt not counted): "
                f"{outcome.detail[:100]}"
            )
            if _is_usage_limit(outcome.detail):
                # The 5-hour window ran out under this agent: wait for it to
                # reset and go on with the queue, as long as that fits before
                # the deadline. This job is not retried this run (its agent
                # started; the next run picks it up, uncharged).
                fresh = budget_check()
                logger.info("Claude usage after the limit hit: %s", budget_guard.usage_summary(fresh))
                session_hit = "session limit" in outcome.detail.lower() or fresh.session_limited
                if session_hit and not fresh.should_stop and _pause_for_session_reset(
                    fresh.session_resets_at,
                    f"Claude session limit hit (job {jid} cut off mid-agent)",
                    deadline, result, worker, left,
                ):
                    consecutive_infra = 0
                    browser_session.close_extra_tabs()
                    continue
                logger.error(
                    "Model usage limit reached; stopping the apply stage with %d job(s) "
                    "untouched so they keep their attempts.",
                    left(),
                )
                result.details.append("ABORTED: model usage limit reached")
                if worker is not None:
                    worker.stop_all("model usage limit reached")
                break
            if consecutive_infra >= MAX_CONSECUTIVE_INFRA_FAILURES:
                logger.error(
                    "%d infrastructure failures in a row; stopping the apply stage.",
                    consecutive_infra,
                )
                result.details.append(
                    f"ABORTED: {consecutive_infra} infrastructure failures in a row"
                )
                break
            if not browser_session.ensure_healthy(constants.browser_profile_dir()):
                logger.error(
                    "Browser could not be recovered; stopping the apply stage with "
                    "%d job(s) untouched so they keep their attempts.",
                    left(),
                )
                result.details.append("ABORTED: browser unrecoverable")
                break
            continue

        consecutive_infra = 0
        consecutive_misreads = 0

        # Durable audit record of exactly what the agent reports it filled in,
        # regardless of outcome - the only record once the live form is gone.
        submission_audit.write_audit_record(
            job_id=jid,
            company=job.company_name or "",
            title=job.job_title or "",
            url=job.job_url,
            status=outcome.status,
            detail=outcome.detail,
            fields_filled=outcome.fields_filled or {},
            resume_path=resume,
            cover_path=cover if cover and Path(cover).is_file() else None,
            attempt_number=job.module4_attempts + 1,
            resume_evidence={
                "upload_name": getattr(outcome, "resume_upload_name", ""),
                "verified_resume_uploads": getattr(outcome, "resume_uploads_ok", 0),
                "easy_apply_seen": getattr(outcome, "easy_apply_seen", False),
                "easy_apply_card_verified": getattr(outcome, "easy_apply_resume_verified", False),
                "rule_violation": getattr(outcome, "resume_rule_violation", ""),
            },
        )
        if outcome.status == "submitted" and getattr(outcome, "resume_rule_violation", ""):
            result.details.append(
                f"job_id={jid}: RESUME RULE VIOLATION ({outcome.resume_rule_violation})")

        if outcome.status == "submitted":
            persist.mark_applied(session, job, dry_run=dry_run)
            result.submitted += 1
            result.details.append(f"job_id={jid}: SUBMITTED ({outcome.detail})")
            logger.info("[%s] SUBMITTED (real application sent).", jid)
            # Pace ONLY after a genuine submission: this delay exists so a
            # burst of real applications doesn't look robotic to ATS
            # platforms. Dead listings, circuit-broken skips and failures
            # submitted nothing, so there is nothing to space out - sleeping
            # after those burned ~2h of a 4h window on the 2026-09-01 run.
            # The wait itself happens before the next job on the same host.
            _note_submission(pace_url)
        elif outcome.status in ("closed", "already_applied"):
            # Terminal but NOT a submission. These were counted into
            # result.submitted until 2026-09-02, which inflated the run
            # summary badly (the 2026-09-01 run reported "Submitted: 44 /
            # Closed: 0" when the truth was 13 submitted + 31 dead listings)
            # and made `Closed:` always print zero. Counted separately now.
            #
            # 2026-09-15: the DB write had the SAME bug for two more weeks -
            # this branch called mark_applied(), which sets applied=1 and
            # status='submitted', so 67 dead listings were stored as real
            # applications. mark_closed() keeps applied=0 and stamps
            # module4_closed_at instead; the job still leaves the queue.
            persist.mark_closed(
                session,
                job,
                status=outcome.status,
                detail=outcome.detail,
                dry_run=dry_run,
            )
            if outcome.status == "closed":
                result.closed += 1
                logger.info("[%s] CLOSED - listing no longer accepting applications.", jid)
            else:
                result.already_applied += 1
                logger.info("[%s] ALREADY APPLIED per the platform.", jid)
            result.details.append(f"job_id={jid}: {outcome.status} ({outcome.detail})")
        elif blockers.dead_listing_reason(outcome.status, outcome.detail, job.job_url):
            # Stuck on a listing the job board no longer serves: closed, like
            # a dead posting the agent recognised itself.
            gone = blockers.dead_listing_reason(outcome.status, outcome.detail, job.job_url)
            persist.mark_closed(session, job, status="closed", detail=f"{gone}: {outcome.detail}",
                                dry_run=dry_run)
            result.closed += 1
            result.details.append(f"job_id={jid}: closed ({gone})")
            logger.info("[%s] CLOSED - %s.", jid, gone)
        elif outcome.status == "blocked_employer":
            # The agent found mid-application that the apply flow routes to a
            # blocklisted destination (Internshala's "Apply on company website"
            # is the motivating case: the LinkedIn company_name is the real
            # employer, so the static blocklist cannot see it in advance).
            # A refusal, not a failure: drop it, charge no attempt. Stamp the
            # status before dropping so the outcome taxonomy sees a real
            # "skipped_by_policy" instead of reading an untouched NULL
            # module4_last_status as not_attempted.
            persist.mark_status(
                session,
                job,
                f"blocked_employer:{outcome.detail[:600]}",
                dry_run=dry_run,
            )
            persist.drop_permanently(session, job, dry_run=dry_run)
            result.blocked += 1
            result.details.append(
                f"job_id={jid}: BLOCKED EMPLOYER at runtime ({outcome.detail[:120]})"
            )
            logger.warning("[%s] Refusing, blocked destination: %s", jid, outcome.detail[:120])
        elif outcome.status == "paused_site" and (
            paused_sites.paused_site_in_text(outcome.detail)
        ):
            site = paused_sites.paused_site_in_text(outcome.detail)
            persist.mark_status(
                session, job, paused_sites.status_for(site, outcome.detail), dry_run=dry_run
            )
            result.paused += 1
            result.details.append(f"job_id={jid}: PAUSED SITE ({site})")
            logger.info("[%s] Paused site %s; job kept for later, no attempt charged.", jid, site)
        elif outcome.status == "captcha_wall" and captcha_walls.gate_host_in_text(outcome.detail):
            wall = captcha_walls.gate_host_in_text(outcome.detail)
            persist.mark_status(
                session, job, captcha_walls.status_for(wall, outcome.detail), dry_run=dry_run
            )
            if captcha_walls.own_host(job.job_url):
                walled.add(captcha_walls.own_host(job.job_url))
            result.captcha_parked += 1
            result.details.append(f"job_id={jid}: PARKED, hCaptcha sign-in wall on {wall}")
            logger.info("[%s] Parked (no attempt charged): hCaptcha sign-in wall on %s.", jid, wall)
        elif outcome.status == "email_apply" and email_apply.mode() != "off" and not dry_run:
            _apply_by_email(session, job, outcome, result, pace_url)
        elif _google_form_wall(outcome, cdp):
            # Not the job's fault and not hopeless: park it, charge nothing.
            form_url = google_forms.find_form_url(outcome.detail) or next(
                (
                    f
                    for f in map(google_forms.find_form_url, google_forms.page_urls(cdp))
                    if f
                ),
                None,
            )
            persist.mark_status(
                session, job, google_forms.status_for(form_url, outcome.detail), dry_run=dry_run
            )
            result.google_signin_parked += 1
            result.details.append(
                f"job_id={jid}: PARKED, Google Form needs a signed-in Google account ({form_url})"
            )
            logger.info("[%s] Parked (no attempt charged): Google Form %s needs sign-in.", jid, form_url)
        else:
            circuit_breaker.record_failure(job.job_url, jid, outcome.detail)
            attempts = persist.record_failed_attempt(session, job, outcome, dry_run=dry_run)
            perm = blockers.permanent_reason(outcome.status, outcome.detail)
            if perm and attempts < constants.max_attempts_per_job():
                persist.drop_permanently(session, job, dry_run=dry_run)
                result.dropped += 1
                result.permanent_blocked += 1
                result.details.append(f"job_id={jid}: DROPPED, not retryable ({perm})")
                logger.warning("[%s] Dropped without retry: %s.", jid, perm)
            elif attempts >= constants.max_attempts_per_job():
                persist.drop_permanently(session, job, dry_run=dry_run)
                result.dropped += 1
                result.details.append(
                    f"job_id={jid}: DROPPED after {attempts} failed attempts "
                    f"({outcome.status}: {outcome.detail[:80]})"
                )
                logger.warning(
                    "[%s] Dropped permanently after %d failed attempts.",
                    jid,
                    attempts,
                )
            result.failed += 1
            result.details.append(f"job_id={jid}: {outcome.status} ({outcome.detail})")

        # Keep the browser tidy across a long run.
        browser_session.close_extra_tabs()

    if deadline_hit:
        logger.warning(
            "Apply stage ended early: deadline. submitted=%d closed=%d failed=%d",
            result.submitted,
            result.closed,
            result.failed,
        )
    if result.circuit_broken:
        logger.warning("Circuit breaker skipped %d job(s) this run.", result.circuit_broken)
    logger.info(
        "Apply stage totals: attempted=%d SUBMITTED=%d closed=%d already_applied=%d "
        "failed=%d dropped=%d (not retryable=%d) paused=%d google_signin_parked=%d "
        "captcha_parked=%d circuit_broken=%d preflight_closed=%d",
        result.attempted,
        result.submitted,
        result.closed,
        result.already_applied,
        result.failed,
        result.dropped,
        result.permanent_blocked,
        result.paused,
        result.google_signin_parked,
        result.captcha_parked,
        result.circuit_broken,
        result.preflight_closed,
    )
