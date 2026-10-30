#!/usr/bin/env python3
"""Park, or put back in the queue, jobs whose form requires a street address.

module4/address_wall.py parks a job whose form will not go on without a street
address or postal code the candidate facts lack (GHO-109): no attempt charged,
skipped by every run until G's answer on GHO-93 is in the candidate facts.

Two modes:

  --backfill   park the jobs that stopped on that wall before the marker
               existed: filed as dropped (should_apply=0) or as a retryable
               stuck. Sets should_apply=1 and module4_last_status to
               'needs_address: <detail>'; attempts are left alone.
  (default)    release every parked job, once the address is in the facts:
               attempts back to 0 and the status cleared, so the next run
               applies to it as a fresh job (no retry notice quoting the old
               "not in the candidate facts" stop).

Safety
------
- Dry run by default; --apply writes. Back up data/jobs.db first.
- Never touches a row that was sent, is closed, or is not a 'stuck' outcome
  (a G skip, a policy skip such as a photo-required form, a blocklisted
  employer).

    uv run python scripts/requeue_address_parked.py --backfill            # preview
    uv run python scripts/requeue_address_parked.py --backfill --apply    # park them
    uv run python scripts/requeue_address_parked.py                       # preview release
    uv run python scripts/requeue_address_parked.py --apply               # release them
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4 import address_wall, blockers  # noqa: E402
from module4.outcome_taxonomy import classify_outcome  # noqa: E402
from policy import employer_blocklist  # noqa: E402


def plan(con: sqlite3.Connection, *, backfill: bool) -> list[tuple[int, str, str | None]]:
    """(job id, description, new status) for every row the mode would change."""
    rows = con.execute(
        "select id, company_name, job_url, should_apply, module4_attempts, module4_last_status "
        "from jobs where coalesce(applied,0)=0 and module4_closed_at is null "
        "and module4_last_status is not null order by id"
    ).fetchall()
    changes = []
    for jid, company, url, should_apply, attempts, last in rows:
        if classify_outcome(applied=False, last_status=last) != "stuck":
            continue
        if employer_blocklist.is_blocked(company, url):
            continue
        if backfill:
            if address_wall.is_parked(last):
                continue
            detail = last.split(":", 1)[-1].strip()
            if not blockers.address_park(detail):
                continue
            changes.append((jid, f"{company}: park (should_apply {should_apply}->1, attempts {attempts} kept)",
                            address_wall.status_for(detail)))
        elif address_wall.is_parked(last):
            changes.append((jid, f"{company}: release (should_apply {should_apply}->1, attempts {attempts}->0)",
                            None))
    return changes


def apply(con: sqlite3.Connection, changes, *, backfill: bool) -> None:
    outcome = "stuck" if backfill else "not_attempted"
    with con:
        for jid, _, status in changes:
            if backfill:
                con.execute("update jobs set should_apply=1, module4_last_status=?, module4_outcome=? where id=?",
                            (status, outcome, jid))
            else:
                con.execute("update jobs set should_apply=1, module4_attempts=0, module4_last_status=null, "
                            "module4_outcome=? where id=?", (outcome, jid))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default=str(REPO / "data" / "jobs.db"))
    ap.add_argument("--backfill", action="store_true", help="park legacy address-wall rows")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    changes = plan(con, backfill=args.backfill)
    for jid, what, _ in changes:
        print(f"  {jid} {what}")
    verb = "parked" if args.backfill else "released"
    if not changes:
        print(f"nothing to be {verb}")
    elif not args.apply:
        print(f"{len(changes)} job(s) would be {verb} (dry run; --apply to write)")
    else:
        apply(con, changes, backfill=args.backfill)
        print(f"{len(changes)} job(s) {verb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
