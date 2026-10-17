#!/usr/bin/env python3
"""the user's 2026-10-11 skip rules: jobboard.example is blocklisted everywhere, and a job
stuck on an ATS account wall is skipped instead of retried.

Offline (no browser, no network). The live-DB part only reads data/jobs.db.
    uv run python scripts/test_skip_walls.py
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from module4 import blockers  # noqa: E402
from policy import employer_blocklist as bl  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:64s} {note}")


@dataclass
class FakeJob:
    id: int
    company_name: str
    job_url: str = "https://www.linkedin.com/jobs/view/1/"
    module4_last_status: str | None = None


# Real stored DETAILs from the 2026-10-11 run (data/logs/apply_audit).
WILLOWBY = (
    "Willowby's Workday application form (willowby.wd1.myworkdayjobs.com) is unresponsive: "
    "clicking \"Create Account,\" \"Sign In,\" and \"Forgot your password?\" all register as successful "
    "clicks but produce no visible state change, no error, and no password-reset email after repeated "
    "attempts, so no account could be created or recovered to proceed past the Create Account/Sign In step."
)
ADATUM = (
    "Adatum Workday application (External_Career_Site_Adatum) - both \"Create Account\" and "
    "\"Forgot your password?\" buttons produce no response/navigation whatsoever despite valid filled "
    "fields and checked consent box; the form is stuck at step 1 of 6 and cannot progress past account creation."
)
WINGTIP = (
    "Wingtip's Avature careers portal (careers.wingtip.com) rejects password Example-Passw0rd! for "
    "alex.example@example.com with \"doesn't meet security requirements\" / \"Password was last changed on "
    "2026-09-18\" (must be different from previous 3 passwords)"
)
ASHFORD = (
    "Ashford's iCIMS/Auth0 portal (login.icims.eu) already has an account for "
    "alex.example@example.com; password reset emails were requested but exhausted the retry limit before a "
    "reset link could be retrieved, so the application form (page 2 onward) was never reached."
)
SHINE = (
    "Jobboard.example's \"Current Salary\" react-dropdown on this job's apply form will not commit any selection "
    "(tried NEED_FIELD x3 and direct trusted clicks on the visible option x2)"
)

# Failures that MUST still get their retry (fixable by budget/handoffs).
RETRYABLE = [
    "ran out of step budget (80 model steps) mid-form, before submitting",
    "Ran out of allotted tool-call iterations mid-form on the \"Application questions\" step of Osprey's Phenom ATS",
    "Resume upload to input#resume_field on Mapleton's custom apply form failed all 4 allowed NEED_UPLOAD attempts",
    "Got to page 3/11 of the LinkedIn Easy Apply flow for Linnet; the Next button stopped responding",
    "Signed in to the Workday account and filled page 2; ran out of steps before Submit",
    "Created the account, verified the email, then the form timed out on the review page",
    "Treyco's Oracle Cloud Recruiting career site gates the application behind an email OTP; no verification "
    "email arrived in the retrieval window",
]


def main() -> int:
    print("1. ATS account walls are skipped, not retried")
    for name, detail in [("Willowby 1464", WILLOWBY), ("Adatum 1513", ADATUM),
                         ("Wingtip 1594", WINGTIP), ("Ashford 1425", ASHFORD)]:
        why = blockers.permanent_reason("stuck", detail)
        check(f"{name} is permanent", bool(why), str(why))
    print("2. Fixable failures keep their retry")
    for d in RETRYABLE:
        why = blockers.permanent_reason("stuck", d)
        check(f"retryable: {d[:58]}", why is None, str(why or ""))

    print("3. jobboard.example is blocklisted (domain, stored status, never the word)")
    check("job URL on jobboard.example", bl.is_blocked("Sample Staffing", "https://www.jobboard.example/jobs/x/123"))
    check("job URL on a jobboard.example subdomain", bl.is_blocked("X", "https://m.jobboard.example/a"))
    check("stored status naming Jobboard.example", bool(bl.job_block_reason(FakeJob(1, "Sample Staffing", module4_last_status="stuck:" + SHINE))))
    check("stored status with www.jobboard.example URL",
          bool(bl.destination_block_reason("stuck: landed on https://www.jobboard.example/myshine/apply")))
    check("company called 'JobBoard Solutions' is NOT blocked", not bl.is_blocked("JobBoard Solutions", ""))
    check("word 'jobboard' in status is NOT blocked", bl.destination_block_reason("stuck: let your skills jobboard") is None)
    check("myjobboard.example is NOT blocked", bl.destination_block_reason("went to myjobboard.example/careers") is None)
    check("jobboard.examplex is NOT blocked", bl.destination_block_reason("see jobboard.examplex/jobs") is None)
    check("normal LinkedIn job is NOT blocked", not bl.is_blocked_job(FakeJob(2, "Rowanby", module4_last_status="stuck: ran out of steps")))
    from module4 import prompts as _prompts  # noqa: E402
    check("agent prompt lists jobboard.example as blocklisted", "**jobboard.example**" in _prompts.SYSTEM_PROMPT)

    print("4. Queue layers refuse a JobBoard job (in-memory DB, no live data touched)")
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from db.models import Base, JobModel
        from module3 import query as m3q
        from module4 import query as m4q

        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        s = sessionmaker(bind=eng)()
        from datetime import datetime
        common = dict(should_apply=True, applied=False, module2_enriched_at=datetime(2026, 10, 1),
                      module3_fit_score=3, source_platform="linkedin")
        s.add_all([
            JobModel(id=10, company_name="Sample Staffing", job_title="FS", job_url="https://www.linkedin.com/jobs/view/10/",
                     module4_attempts=2, module4_last_status="stuck:" + SHINE, **common),
            JobModel(id=11, company_name="Rowanby", job_title="SDE", job_url="https://www.linkedin.com/jobs/view/11/",
                     **common),
        ])
        s.commit()
        ids = {j.id for j in m4q.list_jobs_to_apply(s)}
        check("module4 queue drops the JobBoard job, keeps the normal one", ids == {11}, str(ids))
        ids_f = {j.id for j in m4q.list_jobs_to_apply(s, force=True)}
        check("module4 --force still drops the JobBoard job", 10 not in ids_f, str(ids_f))
        check("module4 --job-id cannot reach the JobBoard job", m4q.list_jobs_to_apply(s, job_id=10, force=True) == [])
        check("module3 --force drops the JobBoard job", 10 not in {j.id for j in m3q.list_jobs_for_module3(s, force=True)})
    except Exception as e:  # pragma: no cover
        check("queue layer checks ran", False, f"{type(e).__name__}: {e}")

    print("5. Queue-reset script will not revive a JobBoard job")
    sys.path.insert(0, str(ROOT / "scripts"))
    from reset_attempts import classify  # noqa: E402
    check("classify(JobBoard status) == blocklist", classify("stuck:" + SHINE, "Sample Staffing", "https://www.linkedin.com/jobs/view/1/") == "blocklist")

    print("6. Live DB, read-only: exactly the expected queued jobs are caught")
    db = ROOT / "data" / "jobs.db"
    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute(
            "select id, company_name, job_url, module4_last_status from jobs where should_apply=1 and applied=0 "
            "and module4_closed_at is null and coalesce(module4_attempts,0)>0"
        ).fetchall()
        perm = {r[0] for r in rows if blockers.permanent_reason_from_last_status(r[3])}
        blocked = {r[0] for r in rows if bl.is_blocked_job(FakeJob(r[0], r[1], r[2], r[3]))}
        expect_perm = {1425, 1464, 1513, 1594} & {r[0] for r in rows}
        check("account-wall jobs still queued are all caught", expect_perm <= perm, f"caught={sorted(perm)}")
        stray = perm - {1425, 1464, 1513, 1594, 1481, 312}
        check("no other queued job is newly treated as permanent", not stray, f"extra={sorted(stray)}")
    else:
        print("  (no live DB on this machine; skipped)")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
