#!/usr/bin/env python3
"""Backfill module4_outcome across every row in data/jobs.db (GHO-2).

module4_outcome is derived purely from (applied, module4_last_status) via
module4/outcome_taxonomy.classify_outcome - the same function module4/persist.py
now calls on every future write, so this script only needs to run once to
cover the rows written before that wiring existed.

Dry-run by default: prints the before/after distribution and a sample of
rows whose outcome would change, writes nothing. Pass --apply to commit.

Usage:
  uv run python scripts/backfill_module4_outcome.py            # dry run
  uv run python scripts/backfill_module4_outcome.py --apply     # write it

Per the working agreement this DB write must land as its own commit,
separate from the code that defines classify_outcome.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import get_session, init_db  # noqa: E402
from db.models import JobModel  # noqa: E402
from module4.outcome_taxonomy import classify_outcome  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the backfill (default: dry run)")
    args = parser.parse_args()

    init_db()
    session = get_session()
    jobs = session.query(JobModel).all()

    before = Counter(j.module4_outcome for j in jobs)
    changes: list[tuple[int, str | None, str]] = []
    for j in jobs:
        new = classify_outcome(applied=bool(j.applied), last_status=j.module4_last_status)
        if j.module4_outcome != new:
            changes.append((j.id, j.module4_outcome, new))
            if args.apply:
                j.module4_outcome = new

    after = Counter(classify_outcome(applied=bool(j.applied), last_status=j.module4_last_status) for j in jobs)

    print(f"Total rows: {len(jobs)}")
    print(f"Rows to change: {len(changes)}")
    print("\nBefore (existing module4_outcome values):")
    for k, v in before.most_common():
        print(f"  {k!r}: {v}")
    print("\nAfter (computed outcome):")
    for k, v in after.most_common():
        print(f"  {k!r}: {v}")
    if sum(after.values()) != len(jobs):
        print("INTERNAL ERROR: after-counts do not sum to total rows", file=sys.stderr)
        return 1

    print(f"\nSample of changed rows (up to 10 of {len(changes)}):")
    for jid, old, new in changes[:10]:
        print(f"  id={jid}: {old!r} -> {new!r}")

    if not args.apply:
        print("\nDRY RUN - no writes made. Re-run with --apply to commit.")
        return 0

    session.commit()
    print(f"\nApplied. {len(changes)} row(s) updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
