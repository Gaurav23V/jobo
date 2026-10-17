"""Read-only cross-source dedup collision report (GHO-41).

Replays every row of a jobs DB through the same dedup keys as
sources/store.py, as if the rows were inserted one by one in id order (each
row's own first_seen_at is "now" for the company+title window). Prints counts
per match kind and a few example id pairs. Ids and kinds only: no company,
title or URL text is printed.

Run: env -u PYTHONPATH uv run python scripts/dedup_report.py --db data/jobs.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sources.store import DedupIndex  # noqa: E402


def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def report(db_path: Path, examples: int = 5) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "select id, job_url, company_name, job_title, first_seen_at from jobs order by id"
    ).fetchall()
    conn.close()

    index = DedupIndex()
    counts: Counter = Counter()
    pairs: dict[str, list[tuple[int, int]]] = {}
    for row_id, job_url, company, title, first_seen in rows:
        seen_at = _ts(first_seen) or datetime.min
        hit = index.match(job_url, company, title, seen_at)
        if hit:
            kind = hit[1]
            counts[kind] += 1
            pairs.setdefault(kind, []).append((hit[0], row_id))
            continue
        if index.company_title_kept_apart(job_url, company, title, seen_at):
            counts["company_title_kept_apart_by_linkedin_id"] += 1
            pairs.setdefault("company_title_kept_apart_by_linkedin_id", []).append((0, row_id))
        index.add(row_id, job_url, company, title, _ts(first_seen))
    return {"rows": len(rows), "counts": dict(counts),
            "examples": {k: v[:examples] for k, v in pairs.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--examples", type=int, default=5)
    args = ap.parse_args()
    out = report(args.db, args.examples)
    dup_total = sum(v for k, v in out["counts"].items() if not k.endswith("kept_apart_by_linkedin_id"))
    print(f"rows: {out['rows']}")
    print(f"would-be duplicates: {dup_total}")
    for kind in ("url", "linkedin_id", "company_title", "company_title_kept_apart_by_linkedin_id"):
        print(f"  {kind}: {out['counts'].get(kind, 0)}")
    for kind, ex in out["examples"].items():
        shown = ", ".join(f"{a}->{b}" if a else str(b) for a, b in ex)
        print(f"  examples {kind} (existing->later id, or later id): {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
