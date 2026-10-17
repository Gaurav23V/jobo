#!/usr/bin/env python3
"""Test the hard employer blocklist (policy/employer_blocklist.py).

Offline, no browser or network needed:
    uv run python scripts/test_employer_blocklist.py

Covers the three things that could make a "hard limit" quietly soft:
  1. every real spelling in the live DB must be caught
  2. incidental substring matches must NOT be caught (the recurring bug class
     in this repo - see reset_attempts.py's phone|sms false positive)
  3. the query layers and the runner gate must all refuse, including under
     --force and --job-id
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy import employer_blocklist as bl  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52s} {note}")


@dataclass
class FakeJob:
    id: int
    company_name: str
    job_url: str = "https://www.linkedin.com/jobs/view/1/"


def main() -> int:
    print("1. Every spelling present in the live DB must be blocked")
    # These three are the actual distinct company_name values in data/jobs.db.
    for name in ("Globex", "Globex in India", "Globex services Pvt Ltd"):
        check(f"blocks {name!r}", bl.is_blocked(name))
    # Plausible future spellings.
    for name in (
        "GLOBEX",
        "globex",
        "Globex Solutions Pvt Ltd",
        "Globex Technology Center",
        "  Globex  ",
        "Globex-India",
    ):
        check(f"blocks {name!r}", bl.is_blocked(name))

    print("\n2. Incidental / lookalike matches must NOT be blocked")
    for name in (
        "Acme Corp",
        "Accent Systems",
        "Globexly Ltd",
        "Centure Labs",
        "Kestrel",
        "",
    ):
        check(f"allows {name!r}", not bl.is_blocked(name))

    print("\n2b. Umbrella (G, 2026-09-05: 'useless and never gonna need it')")
    for name in ("Umbrella", "umbrella", "Umbrella Technologies"):
        check(f"blocks {name!r}", bl.is_blocked(name))
    check(
        "blocks an umbrella.example apply URL under the real employer's name",
        bl.is_blocked("Example Startup", "https://umbrella.example/job/detail/12345"),
        "(the LinkedIn company_name is the employer, not Umbrella)",
    )
    check(
        "does NOT block a lookalike",
        not bl.is_blocked("Agency", "https://notumbrella.example/x"),
    )

    print("\n2c. Initech: permanent employer and domain refusal")
    for name in ("Initech", "InitecH", "INITECH", "Initech India", "Initech Incorporated"):
        check(f"blocks {name!r}", bl.is_blocked(name))
    for url in ("https://initech.example/", "https://careers.initech.example/us/en/job/123"):
        check(f"blocks Initech URL {url!r}", bl.is_blocked("Agency", url))
    for name, url in (("Initechly Ltd", "https://example.com"),
                      ("Agency", "https://notinitech.example/job"),
                      ("Agency", "https://initech.example.org/job")):
        check(f"allows unrelated lookalike {name!r} {url!r}", not bl.is_blocked(name, url))

    print("\n3. Domain matching (laundered company names)")
    check(
        "blocks an globex.example ATS URL under an agency name",
        bl.is_blocked("Some Staffing Agency", "https://careers.globex.example/in/en/job/123"),
    )
    check(
        "blocks a subdomain of globex.example",
        bl.is_blocked("Agency", "https://www.globex.example/careers/job/9"),
    )
    check(
        "does NOT block a lookalike registrable domain",
        not bl.is_blocked("Agency", "https://notglobex.example/jobs/1"),
    )
    check(
        "does NOT block an unrelated domain",
        not bl.is_blocked("Agency", "https://boards.greenhouse.io/x/jobs/1"),
    )
    check("handles a malformed URL without raising", not bl.is_blocked("Agency", "::::"))
    check("handles a None URL", not bl.is_blocked("Agency", None))

    print("\n4. Env var can ADD but never REMOVE the built-in floor")
    import os

    os.environ["JOBO_BLOCKED_EMPLOYERS"] = "Palantir, Foo Corp"
    check("env addition is blocked", bl.is_blocked("Palantir Technologies"))
    check("built-in floor still blocked with env set", bl.is_blocked("Globex"))
    os.environ["JOBO_BLOCKED_EMPLOYERS"] = ""
    check("empty env cannot disable the floor", bl.is_blocked("Globex"))
    os.environ.pop("JOBO_BLOCKED_EMPLOYERS", None)
    check("floor intact with env unset", bl.is_blocked("Globex"))

    print("\n5. partition() splits correctly")
    jobs = [
        FakeJob(1, "Globex in India"),
        FakeJob(2, "Stripe"),
        FakeJob(3, "Globex"),
        FakeJob(4, "Linear"),
    ]
    allowed, blocked = bl.partition(jobs)
    check(
        "partition keeps only non-blocked",
        [j.id for j in allowed] == [2, 4] and [j.id for j in blocked] == [1, 3],
        f"allowed={[j.id for j in allowed]} blocked={[j.id for j in blocked]}",
    )

    print("\n6. Live DB: query layers must refuse, incl. --force and --job-id")
    try:
        from scripts.fixtures.temp_jobs_db import use_temp_db_if_missing

        use_temp_db_if_missing(always=True)  # seeded rows match the example blocklist
        from db.database import get_session
        from module3 import query as m3q
        from module4 import query as m4q

        with get_session() as session:
            from db.models import JobModel

            acc = (
                session.query(JobModel)
                .filter(JobModel.company_name.ilike("%globex%"))
                .all()
            )
            print(f"     ({len(acc)} Globex rows in the test DB)")
            acc_ids = {j.id for j in acc}

            m4 = m4q.list_jobs_to_apply(session)
            check(
                "module4 apply queue contains no Globex",
                not (acc_ids & {j.id for j in m4}),
                f"queue={len(m4)}",
            )
            m4f = m4q.list_jobs_to_apply(session, force=True)
            check(
                "module4 --force contains no Globex",
                not (acc_ids & {j.id for j in m4f}),
                f"queue={len(m4f)}",
            )
            if acc_ids:
                one = int(sorted(acc_ids)[0])
                check(
                    f"module4 --job-id {one} (a Globex job) refuses",
                    m4q.list_jobs_to_apply(session, job_id=one) == [],
                )
                check(
                    f"module4 --job-id {one} --force refuses",
                    m4q.list_jobs_to_apply(session, job_id=one, force=True) == [],
                )
                check(
                    f"module3 --job-id {one} --force refuses",
                    m3q.list_jobs_for_module3(session, job_id=one, force=True) == [],
                )

            m3 = m3q.list_jobs_for_module3(session)
            check(
                "module3 triage queue contains no Globex",
                not (acc_ids & {j.id for j in m3}),
                f"queue={len(m3)}",
            )
            mc = session.query(JobModel).filter(JobModel.company_name.ilike("%initech%")).all()
            mc_ids = {cast(int, j.id) for j in mc}
            check("Initech fixtures exist in live DB", bool(mc_ids))
            check("module4 queue excludes every Initech job", not (mc_ids & {j.id for j in m4}))
            check("module4 --force excludes every Initech job", not (mc_ids & {j.id for j in m4f}))
            check("module3 queue excludes every Initech job", not (mc_ids & {j.id for j in m3}))
            for one in sorted(mc_ids):
                check(f"Initech job {one}: --job-id + --force cannot override block",
                      m4q.list_jobs_to_apply(session, job_id=one, force=True) == [] and
                      m3q.list_jobs_for_module3(session, job_id=one, force=True) == [])
            if mc:
                from unittest.mock import patch
                from module4 import runner, browser_session, agent_driver, budget_guard
                from scripts.reset_attempts import classify
                job = next((j for j in mc if cast(int, j.id) == 595), mc[0])
                before = (job.module4_attempts, job.applied, job.should_apply)
                with patch.object(m4q, "list_jobs_to_apply", return_value=[job]), \
                     patch.object(browser_session, "ensure_browser_running", return_value="http://test.invalid"), \
                     patch.object(browser_session, "ensure_healthy", side_effect=AssertionError("live browser touched")) as healthy, \
                     patch.object(agent_driver, "run_apply_agent", side_effect=AssertionError("agent spawned")) as agent:
                    result = runner.run_module4(
                        session, dry_run=True, budget_check=lambda: budget_guard.decide(None, 1.0)
                    )
                session.refresh(job)
                check("inner runner gate refuses Initech without agent or attempt",
                      result.blocked == 1 and result.attempted == 0 and not agent.called and
                      not healthy.called and before == (job.module4_attempts, job.applied, job.should_apply))
                check("queue-reset classifier will not revive Initech",
                      classify("stuck: ran out of turns", "Initech", str(job.job_url)) == "blocklist")
    except Exception as e:  # pragma: no cover
        check("live DB checks ran", False, f"{type(e).__name__}: {e}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nFailures:")
        for label, ok, note in results:
            if not ok:
                print(f"  {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
