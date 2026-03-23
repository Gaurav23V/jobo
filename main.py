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


if __name__ == "__main__":
    cli()
