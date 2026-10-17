#!/usr/bin/env python
"""Correct historical rows where a dead listing was stored as an application.

Background: until 2026-09-15, module4/runner.py called persist.mark_applied()
for `closed` and `already_applied` outcomes. That sets applied=1,
module4_last_status='submitted' and stamps module4_submitted_at, so dead
postings became indistinguishable from real submissions and every reported
total was inflated.

Ground truth is the per-attempt receipt in data/logs/apply_audit/*.json - the
same source the skill names as authoritative for historical runs. A row is
corrected ONLY when its own final receipt says `closed` or `already_applied`.

Deliberately conservative:
  * Rows with no receipt on disk are NEVER touched. They predate audit
    logging, cannot be proven either way, and are reported separately.
  * A row whose final receipt says `submitted` is left alone.
  * Dry-run by default; --apply writes, and only after taking a backup.

Usage:
  python scripts/backfill_closed_not_applied.py            # dry run
  python scripts/backfill_closed_not_applied.py --apply    # write
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from glob import glob
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "jobs.db"
AUDIT = REPO / "data" / "logs" / "apply_audit"

TERMINAL_NOT_SENT = ("closed", "already_applied")


def load_receipts() -> dict[int, list[tuple[str, str, str]]]:
    """job_id -> [(sort_key, status, detail)], chronological.

    The `timestamp` field inside these files is frequently empty, so ordering
    comes from the filename stamp (job<id>_<YYYYmmdd>_<HHMMSS>.json), falling
    back to mtime. Sorting on the empty field silently mis-ordered attempts on
    a first pass of this backfill.
    """
    out: dict[int, list[tuple[str, str, str]]] = {}
    for path in glob(str(AUDIT / "*.json")):
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        jid = rec.get("job_id")
        if jid is None:
            continue
        stamp = re.search(r"job\d+_(\d{8}_\d{6})", os.path.basename(path))
        key = stamp.group(1) if stamp else f"mtime:{os.path.getmtime(path):.0f}"
        out.setdefault(int(jid), []).append(
            (key, str(rec.get("status")), str(rec.get("detail") or "")[:600])
        )
    for jid in out:
        out[jid].sort()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    args = ap.parse_args()

    if not DB.exists():
        print(f"no database at {DB}")
        return 1

    receipts = load_receipts()
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "module4_closed_at" not in cols:
        if not args.apply:
            print("NOTE: module4_closed_at column is missing; --apply would add it.")
        else:
            conn.execute("ALTER TABLE jobs ADD COLUMN module4_closed_at DATETIME")
            conn.commit()
            print("added column module4_closed_at")

    rows = conn.execute(
        "select id, company_name, job_title, module4_last_status, "
        "module4_submitted_at from jobs where applied = 1"
    ).fetchall()

    fix: list[tuple[int, str, str, str]] = []
    stats = Counter()
    unprovable: list[int] = []

    for row in rows:
        jid = int(row["id"])
        ev = receipts.get(jid)
        if not ev:
            stats["no_receipt_left_alone"] += 1
            unprovable.append(jid)
            continue
        final_status, final_detail = ev[-1][1], ev[-1][2]
        if final_status in TERMINAL_NOT_SENT:
            stats["to_correct"] += 1
            fix.append((jid, str(row["company_name"]), final_status, final_detail))
        else:
            stats[f"confirmed_{final_status}"] += 1

    print(f"applied=1 rows examined: {len(rows)}")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")

    if not fix:
        print("\nnothing to correct.")
        return 0

    print(f"\n{len(fix)} row(s) to correct (receipt says the listing was dead):")
    for jid, co, status, detail in fix[:15]:
        print(f"  {jid:>4} {co[:34]:<34} {status:<16} {detail[:60]}")
    if len(fix) > 15:
        print(f"  ... and {len(fix) - 15} more")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to commit.")
        return 0

    backup = DB.parent / f"jobs.db.backup_closed_backfill_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(DB, backup)
    print(f"\nbackup: {backup}")

    now = datetime.now().isoformat(sep=" ")
    for jid, _co, status, detail in fix:
        conn.execute(
            "update jobs set applied = 0, should_apply = 0, "
            "module4_submitted_at = NULL, module4_closed_at = ?, "
            "module4_last_status = ? where id = ?",
            (now, f"{status}:{detail}" if detail else status, jid),
        )
    conn.commit()

    applied_now = conn.execute(
        "select count(*) from jobs where applied = 1"
    ).fetchone()[0]
    dead_now = conn.execute(
        "select count(*) from jobs where module4_closed_at is not null"
    ).fetchone()[0]
    overlap = conn.execute(
        "select count(*) from jobs where applied = 1 and module4_closed_at is not null"
    ).fetchone()[0]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    print(f"corrected: {len(fix)}")
    print(f"applied=1 now: {applied_now}  (was {len(rows)})")
    print(f"dead listings now: {dead_now}")
    print(f"rows both applied and dead: {overlap} (must be 0)")
    print(f"sqlite integrity_check: {integrity}")
    if unprovable:
        print(
            f"\n{len(unprovable)} applied row(s) have NO receipt on disk and were "
            "left untouched; they predate audit logging and cannot be verified."
        )
    conn.close()
    return 0 if overlap == 0 and integrity == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
