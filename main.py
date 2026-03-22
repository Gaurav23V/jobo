import logging
import sys
from pathlib import Path

# Load environment variables from .env file FIRST (before any other imports)
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

import click
from module1.runner import run
from db.database import init_db, get_session


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
        result = run(session=session, hours=hours, dry_run=dry_run)

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


if __name__ == "__main__":
    cli()
