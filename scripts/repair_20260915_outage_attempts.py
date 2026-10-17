#!/usr/bin/env python
"""Hand back attempts charged during the 2026-09-15 model-provider outage.

The provider was unreachable for ~6 minutes from 03:17. The agent subprocess
died before printing a verdict, so each job persisted as a bare `stuck:` with
NO reason, and the runner charged it a real attempt. 18 jobs (Google, Merlin,
Eastlake, Alpineer among them) lost one of their three attempts for an outage on our
side.

Going forward this cannot recur: parse_verdict() now records the provider
error as the detail, and runner._is_infra_failure() recognises it, so no
attempt is charged. This script repairs the rows charged BEFORE that fix,
which reset_attempts.py cannot classify precisely because the reason was never
written to the DB.

Evidence, not guesswork: a job is repaired only when its own debug transcript
in data/logs/apply_debug/ contains the provider failure AND the stored status
is a bare `stuck:` with no reason. Anything else is left alone.

Usage:
  python scripts/repair_20260915_outage_attempts.py           # dry run
  python scripts/repair_20260915_outage_attempts.py --apply
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from glob import glob
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "jobs.db"
DEBUG = REPO / "data" / "logs" / "apply_debug"

OUTAGE_MARKERS = (
    "can't reach the model provider",
    "cannot reach the model provider",
    "api call failed after",
)
# Only rows that carry NO diagnosis at all. A row with a real reason recorded
# was a real site failure and keeps its attempt.
BARE_STUCK = re.compile(r"^(stuck|captcha_failed):?\s*$")


def transcript_blames_the_provider(job_id: int) -> str | None:
    """Return the provider error line for this job, or None."""
    hits = sorted(glob(str(DEBUG / f"job{job_id}_*")))
    if not hits:
        return None
    try:
        text = Path(hits[-1]).read_text(errors="replace")
    except OSError:
        return None
    low = text.lower()
    if not any(m in low for m in OUTAGE_MARKERS):
        return None
    for line in text.splitlines():
        if any(m in line.lower() for m in OUTAGE_MARKERS):
            return line.strip()[:300]
    return text.strip()[:300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select id, company_name, job_title, module4_attempts, "
        "module4_last_status, applied, should_apply from jobs "
        "where module4_attempts > 0 and applied = 0"
    ).fetchall()

    repair = []
    for row in rows:
        status = str(row["module4_last_status"] or "")
        if not BARE_STUCK.match(status):
            continue
        evidence = transcript_blames_the_provider(int(row["id"]))
        if evidence:
            repair.append((row, evidence))

    if not repair:
        print("nothing to repair.")
        return 0

    print(f"{len(repair)} job(s) charged an attempt for the provider outage:\n")
    for row, evidence in repair:
        print(
            f"  {row['id']:>4} {str(row['company_name'])[:30]:<30} "
            f"attempts={row['module4_attempts']} -> {row['module4_attempts'] - 1}"
        )
    print(f"\nevidence (all {len(repair)}): {repair[0][1][:110]}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return 0

    backup = DB.parent / f"jobs.db.backup_outage_repair_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(DB, backup)
    print(f"\nbackup: {backup}")

    for row, evidence in repair:
        conn.execute(
            "update jobs set module4_attempts = ?, module4_last_status = ? "
            "where id = ?",
            (
                max(0, int(row["module4_attempts"]) - 1),
                f"infra:{evidence}",
                int(row["id"]),
            ),
        )
    conn.commit()

    ids = tuple(int(r["id"]) for r, _ in repair)
    placeholders = ",".join("?" * len(ids))
    after = conn.execute(
        f"select count(*) from jobs where id in ({placeholders}) "
        "and module4_attempts = 0",
        ids,
    ).fetchone()[0]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"repaired: {len(repair)}")
    print(f"now at zero attempts: {after}/{len(repair)}")
    print(f"sqlite integrity_check: {integrity}")
    conn.close()
    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
