#!/usr/bin/env python3
"""Verify the runner's INNER blocklist gate, bypassing the query filter.

The query layer already drops blocked employers, which means the gate inside
runner.py's loop is never exercised by a normal run - so it could rot without
anyone noticing. This drives run_module4 with a monkeypatched query that
deliberately hands it an Globex job, and asserts the loop refuses it
without spawning an agent and without consuming an attempt.

    uv run python scripts/test_blocklist_runner_gate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import get_session  # noqa: E402
from db.models import JobModel  # noqa: E402
from module4 import agent_driver, browser_session, budget_guard, query, runner  # noqa: E402


def main() -> int:
    failures: list[str] = []
    from scripts.fixtures.temp_jobs_db import use_temp_db_if_missing

    # Always the seeded temp DB: the real data/jobs.db holds the user's own
    # employers, while the suite runs with the example person's blocklist.
    use_temp_db_if_missing(always=True)

    with get_session() as session:
        job = (
            session.query(JobModel)
            .filter(JobModel.company_name.ilike("%globex%"))
            .first()
        )
        if job is None:
            print("SKIP: no Globex row in the DB to test with")
            return 0

        before_attempts = job.module4_attempts
        jid = job.id
        print(f"Feeding job {jid} ({job.company_name!r}) straight into run_module4...")

        # Force the runner to receive a blocked job.
        orig_query = query.list_jobs_to_apply
        # Anything below must NOT be reached for a blocked job.
        spawned: list[str] = []

        def boom_agent(*a, **k):
            spawned.append("agent_driver.run_apply_agent")
            raise AssertionError("agent must never be spawned for a blocked employer")

        def boom_browser(*a, **k):
            spawned.append("browser_session.ensure_healthy")
            raise AssertionError("browser must never be touched for a blocked employer")

        query.list_jobs_to_apply = lambda *a, **k: [job]  # type: ignore[assignment]
        runner.query.list_jobs_to_apply = query.list_jobs_to_apply  # type: ignore[attr-defined]
        orig_apply = agent_driver.run_apply_agent
        orig_healthy = browser_session.ensure_healthy
        agent_driver.run_apply_agent = boom_agent  # type: ignore[assignment]
        runner.agent_driver.run_apply_agent = boom_agent  # type: ignore[attr-defined]
        browser_session.ensure_healthy = boom_browser  # type: ignore[assignment]
        runner.browser_session.ensure_healthy = boom_browser  # type: ignore[attr-defined]

        try:
            result = runner.run_module4(
                session, dry_run=True, budget_check=lambda: budget_guard.decide(None, 1.0)
            )
        except AssertionError as e:
            failures.append(f"blocked job reached live machinery: {e}")
            result = None
        finally:
            query.list_jobs_to_apply = orig_query  # type: ignore[assignment]
            runner.query.list_jobs_to_apply = orig_query  # type: ignore[attr-defined]
            agent_driver.run_apply_agent = orig_apply  # type: ignore[assignment]
            runner.agent_driver.run_apply_agent = orig_apply  # type: ignore[attr-defined]
            browser_session.ensure_healthy = orig_healthy  # type: ignore[assignment]
            runner.browser_session.ensure_healthy = orig_healthy  # type: ignore[attr-defined]

        if spawned:
            failures.append(f"these were invoked for a blocked job: {spawned}")

        if result is not None:
            print(f"  result.blocked         = {result.blocked}")
            print(f"  result.attempted       = {result.attempted}")
            print(f"  result.submitted       = {result.submitted}")
            print(f"  result.failed          = {result.failed}")
            for d in result.details:
                print(f"  detail: {d}")
            if result.blocked != 1:
                failures.append(f"expected blocked=1, got {result.blocked}")
            if result.submitted:
                failures.append("a blocked job was counted as submitted")
            if result.failed:
                failures.append("a blocked job was counted as a failure")

        session.refresh(job)
        if int(job.module4_attempts) != int(before_attempts):
            failures.append(
                f"attempt consumed: {before_attempts} -> {job.module4_attempts}"
            )
        else:
            print(f"  attempts unchanged     = {job.module4_attempts} (correct)")

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: runner refuses a blocked employer, no agent, no attempt consumed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
