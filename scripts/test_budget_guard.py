#!/usr/bin/env python3
"""Test module4.budget_guard pure logic (GHO-2).

Network calls (fetch_seven_day_utilization) and the real credential file are
deliberately NOT exercised here - the regression harness forbids remote
network, and this test targets the decision/marker logic that
module4/runner.py actually depends on between jobs.

Run: uv run python scripts/test_budget_guard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import budget_guard  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:60s} {note}")


def main() -> int:
    print("1. decide() threshold logic")
    below = budget_guard.decide(0.74, 0.75)
    check("below threshold does not stop", not below.should_stop)
    at = budget_guard.decide(0.75, 0.75)
    check("exactly at threshold DOES stop (>=, not >)", at.should_stop)
    above = budget_guard.decide(0.90, 0.75)
    check("above threshold stops", above.should_stop)
    unreadable = budget_guard.decide(None, 0.75)
    check("unreadable (None) never stops - unknown is not a breach", not unreadable.should_stop)

    print("\n2. threshold_from_env()")
    import os

    os.environ.pop("JOBO_BUDGET_THRESHOLD", None)
    check("default threshold with no env var", budget_guard.threshold_from_env() == budget_guard.DEFAULT_THRESHOLD)
    os.environ["JOBO_BUDGET_THRESHOLD"] = "0.4"
    check("env var overrides the default", budget_guard.threshold_from_env() == 0.4)
    os.environ["JOBO_BUDGET_THRESHOLD"] = "not-a-number"
    check("malformed env var falls back to default, not a crash", budget_guard.threshold_from_env() == budget_guard.DEFAULT_THRESHOLD)
    os.environ.pop("JOBO_BUDGET_THRESHOLD", None)

    print("\n3. write_resume_marker() produces a readable, complete record")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "nested" / "pipeline_budget_pause.json"
        c = budget_guard.decide(0.81, 0.75)
        budget_guard.write_resume_marker(marker, check=c, stage="apply", jobs_done=12, jobs_remaining=8)
        check("marker file was created (including its parent dir)", marker.is_file())
        data = json.loads(marker.read_text())
        check("records the utilization reading", data.get("utilization") == 0.81)
        check("records the threshold", data.get("threshold") == 0.75)
        check("records which stage stopped", data.get("stage") == "apply")
        check("records progress (done/remaining)", data.get("jobs_done_this_stage") == 12 and data.get("jobs_remaining_this_stage") == 8)
        check("explains how to resume", "re-run" in data.get("resume", "").lower())

    print("\n4. read_access_token() handles a missing/malformed credentials file without raising")
    with tempfile.TemporaryDirectory() as td:
        host = Path(td)
        check("no credentials file at all -> None, not an exception", budget_guard.read_access_token(host) is None)
        (host / ".claude").mkdir()
        (host / ".claude" / ".credentials.json").write_text("not json")
        check("malformed JSON -> None, not an exception", budget_guard.read_access_token(host) is None)
        (host / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": 1}})
        )
        check("expired token -> None (a refresh is Claude Code's job, not ours)", budget_guard.read_access_token(host) is None)
        far_future_ms = 9999999999000
        (host / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": far_future_ms}})
        )
        check("valid, unexpired token is returned", budget_guard.read_access_token(host) == "tok")

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
