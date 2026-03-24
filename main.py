import logging
import sys
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


@click.group()
def cli():
    """Job Application Automation System"""
    pass


@cli.command()
@click.option("--hours", default=24, help="Fetch emails from past N hours")
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
        click.echo(f"  Updated: {result.updated_jobs}")
        if result.errors:
            click.echo(f"  Errors: {len(result.errors)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("enrich")
@click.option("--dry-run", is_flag=True, help="Full pipeline; do not write to the database")
@click.option(
    "--force",
    is_flag=True,
    help="Re-process all jobs (ignore module2_attempted); may hit many URLs",
)
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
def enrich(quiet: bool, dry_run: bool, force: bool):
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
        result = run_enrich(session, dry_run=dry_run, force=force)

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
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
def triage(quiet: bool, dry_run: bool, force: bool, min_score: int | None):
    """Module 3: triage enriched jobs (Gemini fit + resume/cover PDF paths)."""

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
        )

        click.echo("\nSummary:")
        click.echo(f"  Attempted: {result.attempted}")
        click.echo(f"  Phase 1 run: {result.phase1_run}")
        click.echo(f"  Phase 1 skipped: {result.phase1_skipped}")
        click.echo(f"  Phase 2 run: {result.phase2_run}")
        click.echo(f"  Phase 2 skipped: {result.phase2_skipped}")
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


@cli.command("all")
@click.pass_context
def run_all(ctx: click.Context):
    """Run collector then enrich, each with its default options."""

    ctx.invoke(collector, hours=24, dry_run=False, quiet=False)
    ctx.invoke(enrich, dry_run=False, force=False, quiet=False)


if __name__ == "__main__":
    cli()
