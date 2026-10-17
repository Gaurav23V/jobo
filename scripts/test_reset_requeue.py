"""Offline tests for scripts/reset_attempts.py --requeue (GHO-14).

The pending-queue mode only sees should_apply=1 rows, so a job that
module4/blockers.py or max attempts already DROPPED could not be handed back
after its fix landed. --requeue names those jobs one by one. These checks run
on a throwaway SQLite file, never data/jobs.db.

Run: uv run python scripts/test_reset_requeue.py
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location("reset_attempts", REPO / "scripts" / "reset_attempts.py")
reset_attempts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset_attempts)

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


# (id, company, applied, should_apply, attempts, closed_at, outcome, last_status, url)
ROWS = [
    # Dropped by max attempts on our own bug (job 1266, OTP brand name).
    (1266, "Eastlake", 0, 0, 3, None, "stuck",
     'stuck:blocked at final email-verification step: the retrieved code "Eastlake" was 7 chars',
     "https://www.linkedin.com/jobs/view/4000000107/"),
    # Dropped without retry by blockers (job 1489, Google Form wall).
    (1489, "Lamna", 0, 0, 1, None, "stuck",
     "stuck:The Google Form (forms.gle/Ex4mpleF0rmL1nk00) requires Google sign-in to access",
     "https://www.linkedin.com/jobs/view/4000000108/"),
    (2001, "Sent Co", 1, 1, 1, None, "submitted", "submitted", "https://example.com/a"),
    (2002, "Dead Co", 0, 0, 1, "2026-10-01 10:00:00", "closed",
     'closed:page says "no longer accepting applications"', "https://example.com/b"),
    (2003, "Relecloud", 0, 0, 2, None, "skipped_by_policy",
     "skipped:the user chose not to apply (2026-10-04)", "https://example.com/c"),
    (2004, "SAMPLE CONSULTING SERVICES", 0, 0, 0, None, "skipped_by_policy",
     "blocked_employer:apply flow routes through jobboard.example (www.jobboard.example), which is blocklisted",
     "https://www.linkedin.com/jobs/view/1/"),
]


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "create table jobs (id integer primary key, company_name text, applied boolean, "
        "should_apply boolean, module4_attempts integer, module4_closed_at datetime, "
        "module4_outcome text, module4_last_status text, job_url text)"
    )
    conn.executemany("insert into jobs values (?,?,?,?,?,?,?,?,?)", ROWS)
    conn.commit()
    return conn


def _state(conn, jid):
    return conn.execute(
        "select should_apply, module4_attempts, module4_last_status from jobs where id=?", (jid,)
    ).fetchone()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d) / "jobs.db")
        all_ids = ",".join(str(r[0]) for r in ROWS) + ",9999"

        print("\n[1] dry run writes nothing")
        before = [_state(conn, r[0]) for r in ROWS]
        rc = reset_attempts.requeue(conn, all_ids, to=0, apply=False)
        check("dry run exits 0", rc == 0, repr(rc))
        check("no row changed", [_state(conn, r[0]) for r in ROWS] == before)

        print("\n[2] --apply requeues only the dropped, still-open jobs")
        rc = reset_attempts.requeue(conn, all_ids, to=0, apply=True)
        check("apply exits 0", rc == 0, repr(rc))
        for jid in (1266, 1489):
            sa, attempts, status = _state(conn, jid)
            check(f"job {jid}: back in the queue at attempts=0", (sa, attempts) == (1, 0), repr((sa, attempts)))
            check(f"job {jid}: last status kept for the retry notice",
                  status == next(r[7] for r in ROWS if r[0] == jid))
        for jid, why in ((2001, "submitted"), (2002, "closed"), (2003, "skipped by the user"), (2004, "blocklisted")):
            row = next(r for r in ROWS if r[0] == jid)
            check(f"job {jid} ({why}) untouched", _state(conn, jid) == (row[3], row[4], row[7]),
                  repr(_state(conn, jid)))

        print("\n[3] refusal reasons")
        check("submitted refused", reset_attempts.requeue_refusal(1, None, "submitted", "submitted", "", "") is not None)
        check("closed refused", reset_attempts.requeue_refusal(0, "x", "closed", "closed", "", "") is not None)
        check("already_applied refused",
              reset_attempts.requeue_refusal(0, None, "submitted", "already_applied:on file", "", "") is not None)
        check("a plain stuck row is allowed",
              reset_attempts.requeue_refusal(0, None, "stuck", "stuck:ran out of step budget", "Acme", "") is None)

        print("\n[4] bad input")
        check("non-numeric ids rejected", reset_attempts.requeue(conn, "12,abc", to=0, apply=True) == 2)

    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
