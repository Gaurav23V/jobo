#!/usr/bin/env python3
"""Test the honest run verdict (module4.runner.health_verdict).

The bug this prevents: `daily_pipeline.sh` printed "run complete, failed=0"
after the 2026-09-03 run attempted 38 jobs and sent 9, because a stage that
runs but fails every item still exits 0. The shell cannot see per-job
counters, so the verdict is computed where the numbers live.

The counterpart risk is a verdict that cries wolf: a batch that was entirely
blocklisted employers or entirely dead listings did nothing wrong and must
still read as healthy.

Run: uv run python scripts/test_run_verdict.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4.runner import Module4Result, health_verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:54s} {note}")


def main() -> int:
    print("1. The run that started all this must read as DEGRADED")
    # The real 2026-09-03 numbers.
    r = Module4Result(
        attempted=38, submitted=9, closed=1, failed=28, infra_failures=1
    )
    v = health_verdict(r)
    check("2026-09-03 run (9 sent / 28 failed) is NOT healthy", not v.healthy, v.label)
    check("reason names the failure ratio", any("28/37" in x for x in v.reasons),
          v.reasons[0][:52] if v.reasons else "")
    check("reason names the infra failure", any("infrastructure" in x for x in v.reasons))

    print("\n2. Healthy runs must NOT be flagged")
    good = Module4Result(attempted=10, submitted=9, failed=1)
    check("9 of 10 submitted is healthy", health_verdict(good).healthy)

    all_blocked = Module4Result(attempted=0, blocked=7)
    check(
        "an all-blocklisted batch is healthy (we refused on purpose)",
        health_verdict(all_blocked).healthy,
    )

    all_dead = Module4Result(attempted=12, closed=12)
    check(
        "an all-dead-listing batch is healthy (correct no-ops)",
        health_verdict(all_dead).healthy,
    )

    already = Module4Result(attempted=5, already_applied=5)
    check("an all-already-applied batch is healthy", health_verdict(already).healthy)

    empty = Module4Result()
    check("an empty run is healthy (nothing to do)", health_verdict(empty).healthy)

    preflight = Module4Result(attempted=0, preflight_closed=20)
    check("preflight skips alone are healthy", health_verdict(preflight).healthy)

    print("\n3. Genuine trouble must be caught")
    half = Module4Result(attempted=10, submitted=5, failed=5)
    check("50% failure is flagged", not health_verdict(half).healthy)

    just_under = Module4Result(attempted=10, submitted=6, failed=4)
    check("40% failure is not flagged", health_verdict(just_under).healthy)

    infra = Module4Result(attempted=10, submitted=10, failed=0, infra_failures=1)
    check(
        "a browser death is flagged even with everything submitted",
        not health_verdict(infra).healthy,
    )

    cb = Module4Result(attempted=6, submitted=6, circuit_broken=2)
    check("circuit-broken domains are surfaced", not health_verdict(cb).healthy)

    zero_sent = Module4Result(attempted=20, submitted=0, failed=20)
    v2 = health_verdict(zero_sent)
    check("a run that sent nothing is flagged", not v2.healthy, v2.label)

    print("\n4. Blocked jobs never make a run look broken")
    mixed = Module4Result(attempted=5, submitted=5, failed=0, blocked=30)
    check(
        "30 blocked + 5 sent + 0 failed is healthy",
        health_verdict(mixed).healthy,
        "blocked must not count as failure",
    )

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
