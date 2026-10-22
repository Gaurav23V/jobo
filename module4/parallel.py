"""Parallel apply: one LinkedIn worker + N form workers (GHO-5).

One worker averages ~7 minutes per job, so a full queue (60+) cannot clear
inside the 4h apply window. This runs several apply workers side by side,
each its own `jobo apply-worker` process with its own Chromium:

  * the LinkedIn worker keeps the one LinkedIn-signed-in browser (CDP 9333,
    ~/jobo-linkedin-profile). It routes every listing (module4/linkedin_router.py),
    hands employer-site forms to the form workers, and applies the rest
    itself: Easy Apply, Google Forms, anything it could not route.
  * each form worker has a fresh profile that never signs in to LinkedIn
    (~/jobo-form-profile-<n>, CDP 9341+n) and applies only to employer
    forms the LinkedIn worker resolved.

Processes, not threads: the apply path keeps per-job state in module globals
(agent_driver._JOB, form_filler's listing memory) and reads its CDP port from
the environment, so a process per worker isolates all of it for free.

The per-job logic is run_module4's own loop, unchanged: each worker passes a
Worker as its job source, so gates, accounting, the per-job budget check and
the circuit breaker behave exactly as in a single-worker run. Coordination
(claims, routing, exclusivity, the shared stop flag) is module4/apply_queue.py.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from db.models import JobModel
from module4 import apply_queue, browser_session, budget_guard, constants, query, routing

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO / "data" / "logs" / "apply_runs"

ROLE_LINKEDIN = "linkedin"
ROLE_FORM = "form"

FORM_PORT_BASE = 9341
LINKEDIN_PORT = 9333
POLL_SEC = 5.0


def form_port(n: int) -> int:
    return FORM_PORT_BASE + n


def form_profile(n: int) -> Path:
    return Path.home() / f"jobo-form-profile-{n}"


# -- worker side ---------------------------------------------------------------


@dataclasses.dataclass
class Worker:
    """A worker's view of the run; also the job source run_module4 iterates."""

    name: str
    role: str
    queue: apply_queue.ApplyQueue
    form_workers: int = 0
    claim: apply_queue.Claim | None = None
    # 'route' (LinkedIn worker opening a listing to route it), 'apply'
    # (applying to a routed job) or 'direct' (LinkedIn worker applying to an
    # unrouted job because no form worker is left).
    mode: str = "apply"
    poll_sec: float = POLL_SEC

    @classmethod
    def from_env(cls) -> "Worker":
        run_id = apply_queue.current_run_id()
        name = os.environ.get("JOBO_APPLY_WORKER", "").strip()
        role = os.environ.get("JOBO_APPLY_ROLE", "").strip()
        if not run_id or not name or role not in (ROLE_LINKEDIN, ROLE_FORM):
            raise RuntimeError("apply-worker needs JOBO_APPLY_RUN_ID, JOBO_APPLY_WORKER and JOBO_APPLY_ROLE")
        if role == ROLE_FORM:
            # The one rule that must never bend: a form worker never drives
            # the LinkedIn-signed-in browser or its profile.
            if browser_session.cdp_port() == LINKEDIN_PORT:
                raise RuntimeError("form worker refused: CDP port is the LinkedIn browser's")
            if constants.browser_profile_dir().resolve() == (Path.home() / "jobo-linkedin-profile").resolve():
                raise RuntimeError("form worker refused: profile is the LinkedIn profile")
        raw = os.environ.get("JOBO_APPLY_FORM_WORKERS", "0").strip()
        return cls(
            name=name,
            role=role,
            queue=apply_queue.ApplyQueue(run_id),
            form_workers=int(raw) if raw.isdigit() else 0,
        )

    # -- what the runner asks ---------------------------------------------
    @property
    def routing(self) -> bool:
        return self.mode == "route"

    @property
    def apply_url(self) -> str | None:
        """The employer's form URL for a form-routed job, else None (use the listing)."""
        if self.claim and self.claim.route == apply_queue.ROUTE_FORM:
            return self.claim.apply_url
        return None

    @property
    def already_preflighted(self) -> bool:
        """Routed jobs were preflighted when the LinkedIn worker routed them."""
        return bool(self.claim and self.claim.route)

    def remaining(self) -> int:
        c = self.queue.counts()
        return sum(n for k, n in c.items() if k.endswith(":pending") or k.endswith(":resolving"))

    def hand_off(self, job_id: int, route: str, apply_url: str | None, note: str) -> None:
        self.queue.set_route(job_id, route, apply_url, note)
        self.claim = None  # no longer ours; nothing to finish

    def reroute_to_linkedin(self, job_id: int, note: str) -> bool:
        if self.queue.reroute_to_linkedin(job_id, note):
            self.claim = None
            return True
        return False

    def agent_started(self, job_id: int) -> None:
        self.queue.mark_agent_started(job_id)

    def stop_all(self, reason: str) -> None:
        self.queue.request_stop(f"{self.name}: {reason}")

    # -- job source ------------------------------------------------------
    def jobs(self, session: Session) -> Iterator[JobModel]:
        self.queue.register_worker(self.name, self.role, browser_session.cdp_port(), os.getpid())
        try:
            while True:
                stop = self.queue.stop_reason()
                if stop:
                    logger.warning("[%s] Run stop flag set (%s); no more claims.", self.name, stop)
                    return
                claim, mode = self._next_claim()
                if claim is None:
                    if self._finished():
                        return
                    time.sleep(self.poll_sec)
                    continue
                self.claim, self.mode = claim, mode
                session.expire_all()
                job = session.get(JobModel, claim.job_id)
                if job is None or job.applied or job.module4_closed_at is not None or not job.should_apply:
                    # Double-submit guard: whatever another path did to this
                    # row since the queue was built wins.
                    self.queue.finish(claim.job_id, self.name, "skipped: no longer eligible in jobs.db")
                    self.claim = None
                    continue
                try:
                    yield job
                except GeneratorExit:
                    if self.claim is not None:
                        self.queue.release(self.claim.job_id, self.name)
                        self.claim = None
                    raise
                if self.claim is not None:
                    self.queue.finish(self.claim.job_id, self.name)
                    self.claim = None
        finally:
            self.queue.worker_done(self.name)

    def _next_claim(self) -> tuple[apply_queue.Claim | None, str]:
        q = self.queue
        if self.role == ROLE_FORM:
            return q.claim_route(self.name, apply_queue.ROUTE_FORM), "apply"

        forms_alive = bool(q.running_workers(ROLE_FORM))
        if not forms_alive:
            # No form worker left (none configured, or they all stopped):
            # behave like the single-worker runner on everything that's left.
            claim = q.claim_route(self.name, apply_queue.ROUTE_LINKEDIN)
            if claim:
                return claim, "apply"
            claim = q.claim_route(self.name, apply_queue.ROUTE_FORM)
            if claim:
                return claim, "apply"
            return q.claim_unrouted(self.name), "direct"

        # Keep the form workers fed: route ahead until two jobs per form
        # worker are waiting, then spend the time on our own jobs.
        target = max(2 * self.form_workers, 2)
        if q.pending(apply_queue.ROUTE_FORM) < target or not q.pending(apply_queue.ROUTE_LINKEDIN):
            claim = q.claim_unrouted(self.name)
            if claim:
                return claim, "route"
        claim = q.claim_route(self.name, apply_queue.ROUTE_LINKEDIN)
        if claim:
            return claim, "apply"
        return q.claim_unrouted(self.name), "route"

    def _finished(self) -> bool:
        """Nothing claimable now: is there anything left to wait for?"""
        q = self.queue
        if self.role == ROLE_FORM:
            if q.pending(apply_queue.ROUTE_FORM):
                return False  # waiting on exclusivity
            linkedin_alive = bool(q.running_workers(ROLE_LINKEDIN))
            return not (linkedin_alive and q.pending(None))
        # LinkedIn worker: a busy form worker can still hand a job back.
        if q.pending(None) or q.pending(apply_queue.ROUTE_LINKEDIN):
            return False
        if not q.running_workers(ROLE_FORM) and q.pending(apply_queue.ROUTE_FORM):
            return False
        return not q.running_workers(ROLE_FORM)


def result_to_json(result) -> dict:
    return dataclasses.asdict(result)


# -- coordinator side ------------------------------------------------------------


def _merge(results: dict[str, dict]):
    from module4.runner import Module4Result

    total = Module4Result()
    for name, r in results.items():
        for f in dataclasses.fields(Module4Result):
            v = r.get(f.name)
            if f.name == "details":
                total.details.extend(f"[{name}] {d}" for d in (v or []))
            elif isinstance(getattr(total, f.name), bool):
                setattr(total, f.name, getattr(total, f.name) or bool(v))
            elif isinstance(getattr(total, f.name), int):
                setattr(total, f.name, getattr(total, f.name) + int(v or 0))
            elif f.name == "budget_utilization" and v is not None:
                total.budget_utilization = max(total.budget_utilization or 0.0, float(v))
            elif f.name == "linkedin_signed_out_reason" and v:
                total.linkedin_signed_out_reason = v
            elif isinstance(getattr(total, f.name), str) and v:
                # Stop reasons (attach_failed, attach_mismatch, hermes_unusable,
                # agent_browser_mismatch): every worker's reason counts.
                setattr(total, f.name, "; ".join(filter(None, [getattr(total, f.name), v])))
    return total


def _close_browser(port: int) -> None:
    """Close a form worker's Chromium (never the LinkedIn browser)."""
    if port == LINKEDIN_PORT:
        return
    try:
        import urllib.request

        import websocket

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            ws_url = json.loads(r.read().decode("utf-8"))["webSocketDebuggerUrl"]
        ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        ws.close()
    except Exception as e:
        logger.debug("Could not close the browser on port %d: %s", port, e)


def preroute(queue: apply_queue.ApplyQueue, jobs: list[JobModel]) -> int:
    """Queue direct employer links for their worker before any routing.

    A job_url that is already the employer's page (a direct ATS link from
    jobfound, module4/routing.py) needs no LinkedIn routing, so the LinkedIn
    worker's browser never opens it. Returns how many were pre-routed.
    """
    n = 0
    for j in jobs:
        route, apply_url, note = routing.initial_route(j.job_url)
        if route:
            queue.set_route(j.id, route, apply_url, note)
            n += 1
    return n


def run_parallel(
    session: Session,
    *,
    form_workers: int,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    job_id: int | None = None,
    job_ids: list[int] | None = None,
    budget_check=None,
    canary_only: bool = False,
):
    """Coordinator: queue the run, start the workers, wait, merge their results."""
    from module4.runner import Module4Result

    result = Module4Result()
    budget_check = budget_check or (
        lambda: budget_guard.check_budget(
            threshold=budget_guard.threshold_from_env(),
            override=budget_guard.override_from_env(),
        )
    )
    budget = budget_check()
    logger.info("Claude usage at run start: %s", budget_guard.usage_summary(budget))
    if budget.should_stop:
        result.budget_exhausted = True
        result.budget_utilization = budget.utilization
        result.details.append(
            f"BUDGET STOP: weekly utilization {budget.utilization:.0%} >= "
            f"{budget.threshold:.0%} threshold; no worker started"
        )
        logger.warning("Weekly Claude budget at %.0f%%; not starting the parallel apply.", budget.utilization * 100)
        return result
    from module4.runner import hermes_preflight

    if not dry_run and not hermes_preflight(result):
        return result

    jobs = query.list_jobs_to_apply(session, limit=None if job_ids else limit, force=force, job_id=job_id,
                                    retire_blocked=not (dry_run or canary_only))
    if job_ids:
        wanted = set(job_ids)
        jobs = [j for j in jobs if j.id in wanted][: limit or None]
    cap = constants.max_jobs_per_run()
    if cap and not limit:
        jobs = jobs[:cap]
    if not jobs and not canary_only:
        logger.info("No jobs eligible for submission.")
        return result

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
    queue = apply_queue.ApplyQueue(run_id)
    queue.create_run([] if canary_only else [(j.id, j.company_name) for j in jobs])
    if not canary_only:
        preroute(queue, jobs)
    session.rollback()  # the workers own jobs.db from here; hold no read snapshot
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    logger.info(
        "Parallel apply run %s: %d job(s) queued, 1 LinkedIn worker + %d form worker(s).",
        run_id, len(jobs), form_workers,
    )

    base_env = dict(os.environ)
    base_env.update(
        JOBO_APPLY_RUN_ID=run_id,
        JOBO_APPLY_FORM_WORKERS=str(form_workers),
        JOBO_APPLY_DEADLINE_SEC=str(int(constants.stage_deadline_seconds())),
        PYTHONUNBUFFERED="1",
    )
    specs: list[tuple[str, dict]] = [("li", {**base_env, "JOBO_APPLY_WORKER": "li", "JOBO_APPLY_ROLE": ROLE_LINKEDIN})]
    for n in range(1, form_workers + 1):
        specs.append((f"f{n}", {
            **base_env,
            "JOBO_APPLY_WORKER": f"f{n}",
            "JOBO_APPLY_ROLE": ROLE_FORM,
            "JOBO_BROWSER_CDP_PORT": str(form_port(n)),
            "JOBO_BROWSER_PROFILE": str(form_profile(n)),
            "JOBO_LINKEDIN_SESSION_CHECK": "0",
        }))

    # Registered before any worker starts, so the LinkedIn worker's first
    # claim already knows the form workers exist (and routes, not applies).
    for name, env in specs:
        port = int(env.get("JOBO_BROWSER_CDP_PORT") or LINKEDIN_PORT)
        queue.register_worker(name, env["JOBO_APPLY_ROLE"], port, None)

    procs: dict[str, subprocess.Popen] = {}
    argv_tail = ["apply-worker", "--result", ""]
    if dry_run:
        argv_tail.append("--dry-run")
    if force:
        argv_tail.append("--force")
    if canary_only:
        argv_tail.append("--canary-only")
    for i, (name, env) in enumerate(specs):
        argv = [sys.executable, "-m", "main", *argv_tail]
        argv[argv.index("--result") + 1] = str(run_dir / f"{name}.json")
        procs[name] = subprocess.Popen(argv, cwd=str(REPO), env=env)
        logger.info("Started worker %s (pid %d).", name, procs[name].pid)
        if i == 0:
            time.sleep(2)  # the LinkedIn worker registers first, so form workers wait on it

    # A worker that dies without saying so must not leave the others
    # waiting on it, so its exit is recorded in the queue here as well.
    live = dict(procs)
    while live:
        for name, p in list(live.items()):
            rc = p.poll()
            if rc is None:
                continue
            del live[name]
            queue.worker_done(name)
            logger.info("Worker %s exited with code %d.", name, rc)
            if rc != 0:
                result.details.append(f"worker {name} exited with code {rc}")
        time.sleep(1)

    for n in range(1, form_workers + 1):
        _close_browser(form_port(n))

    results: dict[str, dict] = {}
    for name, _ in specs:
        path = run_dir / f"{name}.json"
        try:
            results[name] = json.loads(path.read_text())
        except Exception as e:
            result.details.append(f"worker {name} left no result ({e})")
            result.infra_failures += 1
    merged = _merge(results)
    merged.details = result.details + merged.details
    merged.infra_failures += result.infra_failures

    elapsed_h = (time.monotonic() - started) / 3600
    counts = queue.counts()
    logger.info(
        "Parallel apply run %s finished in %.2fh: submitted=%d (%.1f/h); queue %s",
        run_id, elapsed_h, merged.submitted, merged.submitted / elapsed_h if elapsed_h else 0.0,
        json.dumps(counts, sort_keys=True),
    )
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": run_id,
        "form_workers": form_workers,
        "elapsed_hours": round(elapsed_h, 3),
        "queue": queue.rows(),
        "workers": results,
    }, indent=1, default=str))
    return merged
