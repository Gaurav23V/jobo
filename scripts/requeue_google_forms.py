#!/usr/bin/env python3
"""Put Google Form jobs that were dropped as "sign-in only" back in the queue.

Until 2026-10-03, module4/blockers.py treated "Google Form requires Google
sign-in" as permanent and set should_apply=0. module4/google_forms.py now
parks those jobs instead (no attempt charged, skipped without an agent until
the apply browser is signed in to Google). This script moves the jobs
already dropped into that parked state.

Safety
------
- Dry run by default; --apply writes.
- Touches only rows that are not applied, not closed, and whose stored
  failure is a Google Form sign-in wall (google_forms.is_signin_wall).
- Skips LinkedIn postings that now say "No longer accepting applications"
  (module4.preflight), unless --no-preflight.
- Attempts are reset to 0: the failures were the missing Google sign-in,
  not the job.

    uv run python scripts/requeue_google_forms.py            # preview
    uv run python scripts/requeue_google_forms.py --apply    # do it
    uv run python scripts/requeue_google_forms.py --db /tmp/copy.db --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4 import google_forms, preflight  # noqa: E402

# Form URLs seen in the apply agent's logs for jobs whose DETAIL did not
# carry one (examples; put the real form URLs from your agent logs here).
KNOWN_FORMS = {
    841: "https://docs.google.com/forms/d/e/1FAIpQLSExampleForm02_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform",
    1368: "https://docs.google.com/forms/d/e/1FAIpQLSExampleForm03_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform",
    1379: "https://docs.google.com/forms/d/e/1FAIpQLSExampleForm01_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/viewform",
}


def _guest_api_closed(job_url: str) -> bool:
    m = re.search(r"linkedin\.com/jobs/view/(\d+)", job_url)
    if not m:
        return False
    req = urllib.request.Request(
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}",
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130 Safari/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(600_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    return "closed-job__flavor--closed" in body and "No longer accepting applications" in body


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default=str(REPO / "data" / "jobs.db"))
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--no-preflight", action="store_true", help="skip the LinkedIn closed check")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute(
        "select id, company_name, job_url, should_apply, module4_attempts, module4_last_status "
        "from jobs where coalesce(applied,0)=0 and module4_closed_at is null "
        "and module4_last_status is not null"
    ).fetchall()
    changes = []
    for jid, company, url, should_apply, attempts, last in rows:
        if google_forms.is_parked(last):
            continue
        if not google_forms.is_signin_wall(last.split(":", 1)[-1]):
            continue
        if not args.no_preflight:
            pf = preflight.check_job_open(str(url or ""))
            if pf.should_skip:
                print(f"  skip {jid} {company}: listing closed ({pf.reason})")
                continue
            # /jobs/view/ pages can omit the closed marker (2026-10-03: 233
            # and 286 looked open there but the guest API said closed).
            if _guest_api_closed(str(url or "")):
                print(f"  skip {jid} {company}: LinkedIn guest API says no longer accepting applications")
                continue
        form = google_forms.find_form_url(last) or KNOWN_FORMS.get(jid)
        new_status = google_forms.status_for(form, last.split(":", 1)[-1].strip())
        changes.append((jid, new_status))
        print(f"  {jid} {company}: should_apply {should_apply}->1, attempts {attempts}->0, "
              f"form {form or 'unknown'}")
    if not changes:
        print("nothing to requeue")
        return 0
    if not args.apply:
        print(f"{len(changes)} job(s) would be requeued as parked (dry run; --apply to write)")
        return 0
    with con:
        for jid, st in changes:
            con.execute(
                "update jobs set should_apply=1, module4_attempts=0, module4_last_status=? where id=?",
                (st, jid),
            )
    print(f"{len(changes)} job(s) requeued as parked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
