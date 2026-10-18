import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Load environment variables from .env file FIRST (before any other imports)
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

import click
from db.database import get_session, init_db
from module1.runner import run as run_collector
from module2.runner import run_enrich
from module3.runner import run_module3
from module4 import budget_guard
from module4.funnel import compute_funnel, run_window_from_log
from module4.runner import health_verdict, run_module4
from sources import jobfound


@click.group()
def cli():
    """Job Application Automation System"""
    pass


@cli.command()
@click.option("--hours", default=168, help="Fetch emails from past N hours (default: 7 days)")
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
@click.option("--dry-run", is_flag=True, help="Parse and log without writing to DB")
def collector(hours: int, quiet: bool, dry_run: bool):
    """Collect job-alert emails from Gmail and store in database"""

    if quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    try:
        # Initialize DB and create session in main.py (infrastructure setup)
        init_db()
        session = get_session()

        # Pass session to the module runner
        result = run_collector(session=session, hours=hours, dry_run=dry_run)

        click.echo(f"\nSummary:")
        click.echo(f"  Emails processed: {result.emails_processed}")
        click.echo(f"  Jobs extracted: {result.jobs_extracted}")
        click.echo(f"  New jobs: {result.new_jobs}")
        click.echo(f"  Duplicates skipped: {result.duplicate_jobs}")
        if result.errors:
            click.echo(f"  Errors: {len(result.errors)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("collect-jobfound")
@click.option("--dry-run", is_flag=True, help="Fetch and filter; write nothing (not even the cursor)")
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
@click.option(
    "--since-hours",
    default=jobfound.DEFAULT_SINCE_HOURS,
    show_default=True,
    help="With no stored cursor, how far back the sitemap gap fill looks",
)
@click.option("--no-gap-fill", is_flag=True, help="Feed only: skip the sitemap gap fill (cursor unchanged)")
@click.option(
    "--force-gap-fill",
    is_flag=True,
    hidden=True,
    help="Fetch every sitemap job page in the window, even ones the feed has (end-to-end checks)",
)
def collect_jobfound(dry_run: bool, quiet: bool, since_hours: int, no_gap_fill: bool, force_gap_fill: bool):
    """Collect jobs from jobfound.org (Atom feed + sitemap gap fill), pre-enriched"""

    if quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    if not dry_run:
        init_db()
    session = get_session()
    result = jobfound.collect(
        session,
        dry_run=dry_run,
        since_hours=since_hours,
        gap_fill=not no_gap_fill,
        force_gap_fill=force_gap_fill,
    )

    ins = result.insert
    click.echo(f"\nSummary{' (dry run, nothing written)' if dry_run else ''}:")
    click.echo(f"  Feed entries: {result.feed_entries}" + (f" (FAILED: {result.feed_error})" if result.feed_error else ""))
    if result.parse_skipped:
        click.echo(f"  Parse skipped: {result.parse_skipped}")
    if not no_gap_fill:
        click.echo(
            f"  Sitemap gap URLs: {result.gap_urls}"
            + (f" (FAILED: {result.sitemap_error})" if result.sitemap_error else "")
            + (f", {result.gap_truncated} left for next run" if result.gap_truncated else "")
        )
    click.echo(f"  Pages fetched: {result.pages_fetched} (errors: {result.page_errors})")
    click.echo(f"  Thin-description fallbacks: {result.thin_fallbacks} (failed: {result.thin_fallback_errors})")
    click.echo(f"  Kept: {result.kept} (page-URL fallbacks: {result.page_url_fallbacks})")
    click.echo(f"  Dropped: {sum(result.dropped_by_reason.values())}")
    for reason, n in sorted(result.dropped_by_reason.items(), key=lambda kv: -kv[1]):
        click.echo(f"    {reason}: {n}")
    click.echo(f"  {'Would insert' if dry_run else 'Inserted'}: {ins.inserted}")
    click.echo(f"  Duplicates: {ins.duplicates} {ins.duplicates_by or ''}".rstrip())
    if ins.errors:
        click.echo(f"  Insert errors: {len(ins.errors)} ({ins.errors[0]})")
    if result.cursor_after:
        click.echo(f"  Cursor: {result.cursor_before} -> {result.cursor_after}")
    if result.failed:
        sys.exit(1)


@cli.command("enrich")
@click.option("--dry-run", is_flag=True, help="Full pipeline; do not write to the database")
@click.option(
    "--force",
    is_flag=True,
    help="Re-process all jobs (ignore module2_attempted); may hit many URLs",
)
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
@click.option(
    "--job-id",
    type=int,
    default=None,
    help="Restrict to a single job ID (add --force to retry a failed scrape)",
)
def enrich(quiet: bool, dry_run: bool, force: bool, job_id: int | None = None):
    """Enrich job rows from LinkedIn pages via Playwright + Ollama."""

    if quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        # Per-request "HTTP Request ... 200 OK" is noise; real failures surface via module2 logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    try:
        init_db()
        session = get_session()
        result = run_enrich(session, dry_run=dry_run, force=force, job_id=job_id)

        click.echo("\nSummary:")
        click.echo(f"  Attempted: {result.attempted}")
        click.echo(f"  Succeeded: {result.succeeded}")
        click.echo(f"  Failed: {result.failed}")
        if result.errors:
            click.echo(f"  Error lines: {len(result.errors)}")
            for e in result.errors[:20]:
                click.echo(f"    - {e}")
            if len(result.errors) > 20:
                click.echo(f"    ... and {len(result.errors) - 20} more")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("prefilter")
@click.option("--dry-run", is_flag=True, help="Count what would be dropped; write nothing")
@click.option("--quiet", is_flag=True, help="Suppress per-row log lines")
def prefilter(dry_run: bool, quiet: bool):
    """Rule filter between enrich and triage (policy/job_filters.py): drop rows before a triage call."""
    from module3.prefilter import run_prefilter

    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    try:
        init_db()
        session = get_session()
        result = run_prefilter(session, dry_run=dry_run)
        click.echo("\n" + result.render(dry_run=dry_run))
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("triage")
@click.option("--dry-run", is_flag=True, help="Run pipeline; do not write to the database")
@click.option(
    "--force",
    is_flag=True,
    help="Ignore idempotency; re-run fit and/or materials for enriched jobs",
)
@click.option(
    "--min-score",
    type=int,
    default=None,
    help="Minimum fit score (0-5) to set should_apply; overrides JOBO_MIN_FIT_SCORE / default",
)
@click.option(
    "--job-id",
    type=int,
    default=None,
    help="Restrict to a single job ID (for one-off testing); idempotency rules still apply unless --force",
)
@click.option(
    "--queued",
    is_flag=True,
    help="Re-score and regenerate materials for jobs in the apply queue only "
    "(should_apply, not applied, not closed); use after a resume/profile change",
)
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
def triage(
    quiet: bool,
    dry_run: bool,
    force: bool,
    min_score: int | None,
    job_id: int | None,
    queued: bool,
):
    """Module 3: triage enriched jobs (Claude Code fit + resume/cover PDF paths)."""
    from policy import personal
    personal.require_real("triage")

    if min_score is not None and not (0 <= min_score <= 5):
        click.echo("Error: --min-score must be between 0 and 5.", err=True)
        raise click.Abort()

    if quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    try:
        init_db()
        session = get_session()
        result = run_module3(
            session,
            dry_run=dry_run,
            force=force,
            min_score=min_score,
            job_id=job_id,
            queued=queued,
        )

        click.echo("\nSummary:")
        click.echo(f"  Attempted: {result.attempted}")
        click.echo(f"  Phase 1 run: {result.phase1_run}")
        click.echo(f"  Phase 1 skipped: {result.phase1_skipped}")
        click.echo(f"  Phase 2 run: {result.phase2_run}")
        click.echo(f"  Phase 2 skipped: {result.phase2_skipped}")
        if result.phase2_closed:
            click.echo(f"  Phase 2 closed (dead listing, no materials made): {result.phase2_closed}")
        click.echo(f"  Failed: {result.failed}")
        if result.llm_usage:
            click.echo(f"  LLM usage: {result.llm_usage}")
        if result.errors:
            click.echo(f"  Error lines: {len(result.errors)}")
            for e in result.errors[:20]:
                click.echo(f"    - {e}")
            if len(result.errors) > 20:
                click.echo(f"    ... and {len(result.errors) - 20} more")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("all")
@click.pass_context
def run_all(ctx: click.Context):
    """Run collector then enrich, each with its default options."""

    ctx.invoke(collector, hours=168, dry_run=False, quiet=False)
    ctx.invoke(enrich, dry_run=False, force=False, quiet=False)


@cli.command("apply")
@click.option("--dry-run", is_flag=True, help="Do not write submission state to DB")
@click.option("--limit", type=int, default=None, help="Max jobs to attempt this run")
@click.option("--force", is_flag=True, help="Retry jobs even after failed attempts")
@click.option(
    "--job-id",
    type=int,
    default=None,
    help="Restrict to a single job ID (for one-off testing); job must already have should_apply=1",
)
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
@click.option(
    "--form-workers",
    type=click.IntRange(0, 5),
    default=0,
    show_default=True,
    help="Parallel apply (module4/parallel.py): one LinkedIn worker plus this many form "
    "workers, each with its own browser. 0 = the single-worker runner.",
)
@click.option(
    "--job-ids",
    default="",
    help="Parallel apply only: restrict the run to these comma-separated job IDs "
    "(a small proof batch); queue rules still apply.",
)
@click.option(
    "--canary-only",
    is_flag=True,
    help="Run each worker's attach canary (GHO-17) and stop: no job is claimed and "
    "nothing is applied. --dry-run is NOT that: it still runs real apply agents.",
)
@click.option(
    "--plan",
    is_flag=True,
    help="Print how each queued job would be routed (ATS kind, blocklist, paused site, "
    "preflight, which worker) and exit. No browser, agent, network or DB write. Takes "
    "--job-id, --limit and --force.",
)
def apply(
    quiet: bool, dry_run: bool, limit: int | None, force: bool, job_id: int | None,
    form_workers: int, job_ids: str, canary_only: bool, plan: bool,
):
    """Module 4: submit applications for should_apply jobs (Hermes browser agent)."""
    if plan:
        from module4 import plan as plan_mod

        logging.basicConfig(level=logging.WARNING)
        # No init_db(): it may ALTER jobs.db, and the plan writes nothing.
        missing = plan_mod.missing_columns()
        if missing:
            raise click.ClickException(
                f"jobs.db lacks column(s) {', '.join(missing)}; run `jobo prefilter --dry-run` "
                "on it once (adds them) and retry"
            )
        session = get_session()
        click.echo(plan_mod.render(plan_mod.build_plan(session, job_id=job_id, limit=limit, force=force)))
        return

    from policy import personal
    personal.require_real("apply")
    ids = [int(x) for x in job_ids.replace(" ", "").split(",") if x]
    if ids and not form_workers:
        raise click.UsageError("--job-ids needs --form-workers (single-worker runs take --job-id)")

    if quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    try:
        init_db()
        session = get_session()
        if form_workers:
            from module4.parallel import run_parallel

            result = run_parallel(
                session, form_workers=form_workers, dry_run=dry_run, limit=limit, force=force,
                job_id=job_id, job_ids=ids or None, canary_only=canary_only,
            )
        else:
            result = run_module4(session, dry_run=dry_run, limit=limit, force=force, job_id=job_id,
                                 canary_only=canary_only)

        click.echo("\nSummary:")
        click.echo(f"  Attempted:          {result.attempted}")
        click.echo(f"  APPLICATIONS SENT:  {result.submitted}   <- real submissions")
        click.echo(f"  Closed (dead job):  {result.closed}")
        click.echo(f"  Already applied:    {result.already_applied}")
        click.echo(f"  Failed:             {result.failed}")
        if result.dropped:
            click.echo(f"    of which dropped: {result.dropped} (hit max attempts or not retryable)")
        if result.permanent_blocked:
            click.echo(
                f"  Not retryable:      {result.permanent_blocked} "
                "(captcha, missing DOB/PAN, Google-only sign-in, dead account; dropped)"
            )
        if result.paused:
            click.echo(
                f"  Paused sites:       {result.paused} "
                "(policy/paused_sites.py; kept for later, no attempt charged)"
            )
        if result.captcha_parked:
            click.echo(
                f"  hCaptcha walls:     {result.captcha_parked} "
                "(module4/captcha_walls.py; parked, no attempt charged)"
            )
        if result.circuit_broken:
            click.echo(f"  Circuit-broken:     {result.circuit_broken}")
        if form_workers:
            click.echo(
                f"  Routed:             {result.routed_form} to form workers, "
                f"{result.routed_linkedin} kept by the LinkedIn worker, "
                f"{result.rerouted} handed back"
            )
        if result.preflight_closed:
            click.echo(
                f"  Preflight-skipped:  {result.preflight_closed} "
                "(detected dead before spawning an agent)"
            )
        if result.infra_failures:
            click.echo(
                f"  Infra failures:     {result.infra_failures} "
                "(our browser broke; attempts NOT counted against these jobs)"
            )
        if result.blocked:
            click.echo(
                f"  Blocked employers:  {result.blocked} "
                "(hard blocklist; never attempted, no attempt consumed)"
            )
        if result.budget_exhausted:
            click.echo(
                f"  BUDGET STOP:        weekly utilization {result.budget_utilization:.0%} "
                "reached the stop threshold; stage ended cleanly, marker written "
                f"to {budget_guard.RESUME_MARKER_PATH}"
            )
        if result.session_pauses or result.session_limit_stop:
            click.echo(
                f"  5h SESSION LIMIT:   {result.session_pauses} pause(s) until the window reset"
                + (f"; stopped: {result.session_limit_stop}" if result.session_limit_stop else "")
            )
        if result.details:
            click.echo("  Details:")
            for d in result.details[:80]:
                click.echo(f"    - {d}")

        # Honest exit status. A stage that ran but failed EVERY item used to
        # exit 0, so daily_pipeline.sh printed "run complete, failed=0" after
        # the 2026-09-03 run sent 9 of 38 - and nobody noticed for a day.
        # The shell cannot see these counters, so the verdict is computed here
        # where the numbers actually live.
        #
        # Deliberately NOT counting as unhealthy: closed/already_applied
        # (correct no-ops), blocked employers (we refused on purpose), and
        # preflight skips. Only genuine failures and infra breakage.
        verdict = health_verdict(result)
        click.echo(f"\n  HEALTH: {verdict.label}")
        if verdict.reasons:
            for r in verdict.reasons:
                click.echo(f"    ! {r}")
        if not verdict.healthy:
            # Non-zero so the pipeline's own summary line stops lying. 3 is
            # DEGRADED there; anything else reads as FAILED (attach canary).
            raise SystemExit(4 if verdict.label == "FAILED" else 3)

    except SystemExit:
        raise
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("apply-worker", hidden=True)
@click.option("--result", "result_path", type=click.Path(path_type=Path), required=True)
@click.option("--dry-run", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--canary-only", is_flag=True)
def apply_worker(result_path: Path, dry_run: bool, force: bool, canary_only: bool):
    """One worker of a parallel apply run; started by `jobo apply --form-workers N`."""
    from module4.parallel import Worker, result_to_json

    worker = Worker.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s - [{worker.name}] %(name)s - %(levelname)s - %(message)s",
    )
    init_db()
    session = get_session()
    result = run_module4(session, dry_run=dry_run, force=force, worker=worker, canary_only=canary_only)
    result_path.write_text(json.dumps(result_to_json(result), default=str))


def _parse_when(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise click.BadParameter(f"could not parse {value!r} as a date/datetime")


@cli.command("funnel")
@click.option(
    "--run-log",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Print the funnel for one pipeline run, using that run's own log file "
    "(data/logs/run_*.log) to bound the window.",
)
@click.option("--since", default=None, help="Window start (YYYY-MM-DD[ HH:MM:SS]); default 7 days ago")
@click.option("--until", default=None, help="Window end (YYYY-MM-DD[ HH:MM:SS]); default now")
def funnel(run_log: Path | None, since: str | None, until: str | None):
    """The apply-rate funnel: of should_apply=1 jobs still open, share submitted."""

    if run_log and (since or until):
        raise click.UsageError("--run-log is exclusive with --since/--until")

    init_db()
    session = get_session()

    if run_log:
        start, end = run_window_from_log(run_log)
        result = compute_funnel(session, since=start, until=end + timedelta(seconds=1))
        click.echo(result.render(f"Funnel for run log {run_log}"))
        return

    since_dt = _parse_when(since) if since else datetime.now() - timedelta(days=7)
    until_dt = _parse_when(until) if until else datetime.now()
    result = compute_funnel(session, since=since_dt, until=until_dt)
    click.echo(result.render("Funnel for date range"))


@cli.command("run-report")
@click.option(
    "--run-log",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="The pipeline run's log (data/logs/run_*.log)",
)
@click.option("--json", "as_json", is_flag=True, help="Print the whole report as one JSON object")
@click.option(
    "--verdicts",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="pipeline_verdicts.jsonl to read (default: next to the run log)",
)
def run_report(run_log: Path, as_json: bool, verdicts: Path | None):
    """Post-run triage report: verdicts, apply rate, failure clusters (GHO-44).

    Read-only on jobs, except that it upserts this run's failure-ledger counts
    (idempotent per log).
    """
    from module4 import run_report as rr

    init_db()
    report = rr.build_report(get_session(), run_log, verdicts_path=verdicts)
    if as_json:
        click.echo(json.dumps(report.to_json(), indent=2, default=str))
    else:
        click.echo(rr.render_text(report))


@cli.group("ledger")
def ledger():
    """The failure ledger: one row per (failure signature, ATS host)."""


@ledger.command("set")
@click.argument("signature")
@click.option("--host", default=None, help="ATS host; omit to update every host with this signature")
@click.option("--bucket", type=click.Choice(["temporary", "fixable", "needs_g"]), default=None)
@click.option("--issue", default=None, help="Paperclip identifier, e.g. GHO-57")
@click.option("--fix-commit", default=None)
@click.option(
    "--status",
    type=click.Choice(["open", "fixing", "fixed", "wont_fix", "needs_g"]),
    default=None,
)
def ledger_set(signature, host, bucket, issue, fix_commit, status):
    """Record triage or a fix for a signature (wont_fix is recorded only)."""
    from module4 import run_report as rr

    init_db()
    try:
        entries = rr.ledger_set(
            get_session(), signature, host=host, bucket=bucket, issue=issue,
            fix_commit=fix_commit, status=status,
        )
    except LookupError as exc:
        raise click.UsageError(str(exc))
    click.echo(rr.render_ledger(entries))


@ledger.command("list")
@click.option(
    "--status",
    type=click.Choice(["open", "fixing", "fixed", "wont_fix", "needs_g"]),
    default=None,
)
@click.option("--json", "as_json", is_flag=True)
def ledger_list(status, as_json):
    """List ledger entries, most recently seen first."""
    from module4 import run_report as rr

    init_db()
    entries = rr.ledger_list(get_session(), status)
    if as_json:
        click.echo(json.dumps([rr.ledger_dict(e) for e in entries], indent=2))
    else:
        click.echo(rr.render_ledger(entries))


@cli.command("budget-gate")
@click.option(
    "--stage",
    required=True,
    help="Which gate this is, for the log/verdict (e.g. pipeline, triage)",
)
@click.option(
    "--override",
    is_flag=True,
    help=f"The owner explicitly asked for this run: proceed whatever the usage "
    f"(same as {budget_guard.OVERRIDE_ENV}=1)",
)
def budget_gate(stage: str, override: bool):
    """Weekly Claude budget gate for scripts/daily_pipeline.sh.

    Exit 0 = proceed (including when usage is unreadable), exit
    75 = at/over JOBO_BUDGET_THRESHOLD (default 0.50) weekly, or at/over
    JOBO_SESSION_THRESHOLD (default 0.30) of the 5-hour session
    (the account is shared with other work; the override does not lift this), do not run. stdout
    is one JSON object for the verdict line; stderr is one human log line.
    """

    check = budget_guard.check_budget(
        threshold=budget_guard.threshold_from_env(),
        override=override or budget_guard.override_from_env(),
    )
    click.echo(budget_guard.gate_message(check, stage), err=True)
    click.echo(json.dumps(budget_guard.gate_record(check, stage)))
    stop = check.should_stop or check.session_limited
    sys.exit(budget_guard.GATE_STOP_EXIT if stop else 0)


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Print the checks as JSON")
@click.option(
    "--offline",
    is_flag=True,
    help="Report items that need a real login (Gmail OAuth client/token, LinkedIn "
    "session, Hermes profile) as SKIP instead of checking them (tests, fresh clones)",
)
def doctor(as_json: bool, offline: bool):
    """Check this machine is ready to run jobo: one line per item, with a fix.

    Exit 0 when nothing is MISSING (WARN items are optional, SKIP items were
    not checked). Never prints the account password.
    """
    from jobo_setup import doctor as checks_mod

    checks = checks_mod.run_checks(offline=offline)
    render = checks_mod.to_json if as_json else checks_mod.render
    click.echo(render(checks, offline=offline))
    sys.exit(checks_mod.exit_code(checks))


if __name__ == "__main__":
    cli()
