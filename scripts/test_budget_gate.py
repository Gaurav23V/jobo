#!/usr/bin/env python3
"""Test the whole-run weekly budget gate decisions (GHO-3).

scripts/daily_pipeline.sh calls `jobo budget-gate` before the collector and
again before triage; this suite covers the pure logic behind it - skip,
proceed, unreadable, and the user's manual override - without any network (the
regression harness refuses it). scripts/test_pipeline_budget_gate.sh drives
the real shell script end to end with stubbed stages.

Run: uv run python scripts/test_budget_gate.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import budget_guard  # noqa: E402

results: list[tuple[str, bool, str]] = []
RESETS = "2026-10-15T11:00:00.364006+00:00"


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:64s} {note}")


def main() -> int:
    print("1. Skip path: at/over the threshold")
    over = budget_guard.decide(0.80, 0.75, resets_at=RESETS)
    rec = budget_guard.gate_record(over, "pipeline")
    msg = budget_guard.gate_message(over, "pipeline")
    check("80% >= 75% stops", over.should_stop)
    check("record decision is SKIP", rec["decision"] == "SKIP")
    check("record keeps utilization, threshold and reset time",
          rec["utilization"] == 0.80 and rec["threshold"] == 0.75 and rec["resets_at"] == RESETS)
    check("record names the gate", rec["gate"] == "pipeline")
    check("log line says SKIPPING with usage and reset time",
          "SKIPPING" in msg and "80%" in msg and RESETS in msg, msg[:70])
    check("log line documents the override", budget_guard.OVERRIDE_ENV in msg and "--budget-override" in msg)
    check("exactly at threshold also stops", budget_guard.decide(0.75, 0.75).should_stop)

    print("\n2. Proceed path: below the threshold")
    under = budget_guard.decide(0.56, 0.75, resets_at=RESETS)
    rec = budget_guard.gate_record(under, "triage")
    msg = budget_guard.gate_message(under, "triage")
    check("56% < 75% proceeds", not under.should_stop)
    check("record decision is PROCEED", rec["decision"] == "PROCEED")
    check("log line says proceeding", "proceeding" in msg and "56%" in msg, msg[:70])

    print("\n3. Override: the user explicitly asked for this run")
    forced = budget_guard.decide(0.90, 0.75, resets_at=RESETS, override=True)
    rec = budget_guard.gate_record(forced, "pipeline")
    msg = budget_guard.gate_message(forced, "pipeline")
    check("override proceeds even at 90%", not forced.should_stop)
    check("record decision is OVERRIDE and flags it", rec["decision"] == "OVERRIDE" and rec["override"] is True)
    check("log line says the override was used", budget_guard.OVERRIDE_ENV in msg and "proceeding" in msg, msg[:70])
    below_forced = budget_guard.gate_record(budget_guard.decide(0.10, 0.75, override=True), "pipeline")
    check("override below the threshold is a plain PROCEED", below_forced["decision"] == "PROCEED")

    print("\n4. Unreadable usage keeps today's behaviour (proceed) and says why")
    unknown = budget_guard.decide(None, 0.75, error="usage endpoint unreadable: URLError")
    rec = budget_guard.gate_record(unknown, "pipeline")
    msg = budget_guard.gate_message(unknown, "pipeline")
    check("None never stops", not unknown.should_stop)
    check("record decision is PROCEED_UNREADABLE with the error",
          rec["decision"] == "PROCEED_UNREADABLE" and "URLError" in (rec["error"] or ""))
    check("log line says UNREADABLE and the reason", "UNREADABLE" in msg and "URLError" in msg, msg[:70])
    with_tokenless = budget_guard.check_budget(threshold=0.75, host_home=Path("/nonexistent-gho3"))
    check("no credentials file -> unreadable with an error, no network",
          with_tokenless.utilization is None and "no unexpired OAuth token" in (with_tokenless.error or ""))

    print("\n5. parse_usage_payload() reads the live endpoint's shape")
    live_shape = {"five_hour": {"utilization": 14.0}, "seven_day": {"utilization": 56.0, "resets_at": RESETS}}
    check("seven_day utilization + resets_at", budget_guard.parse_usage_payload(live_shape) == (0.56, RESETS, None))
    util, resets, err = budget_guard.parse_usage_payload({"seven_day": {"utilization": None, "resets_at": RESETS}})
    check("null utilization -> None with an error, reset time kept", util is None and resets == RESETS and bool(err))
    util, _, err = budget_guard.parse_usage_payload(["not", "a", "dict"])
    check("non-object payload -> None with an error, no exception", util is None and bool(err))

    print("\n6. override_from_env()")
    saved = os.environ.pop(budget_guard.OVERRIDE_ENV, None)
    try:
        check("unset -> no override", not budget_guard.override_from_env())
        for value in ("1", "true", "YES"):
            os.environ[budget_guard.OVERRIDE_ENV] = value
            check(f"{value!r} -> override", budget_guard.override_from_env())
        for value in ("0", "", "no"):
            os.environ[budget_guard.OVERRIDE_ENV] = value
            check(f"{value!r} -> no override", not budget_guard.override_from_env())
    finally:
        os.environ.pop(budget_guard.OVERRIDE_ENV, None)
        if saved is not None:
            os.environ[budget_guard.OVERRIDE_ENV] = saved

    print("\n7. The gate's stop exit status is distinct from success and crashes")
    print("\n6. 5-hour session line (stop at 30%)")
    sess = budget_guard.decide(0.13, 0.50, session_utilization=0.31, session_threshold=0.30)
    rec = budget_guard.gate_record(sess, "pipeline")
    msg = budget_guard.gate_message(sess, "pipeline")
    check("31% session >= 30% is session_limited", sess.session_limited)
    check("record decision is SKIP_SESSION", rec["decision"] == "SKIP_SESSION")
    check("log line says SKIPPING with the session %", "SKIPPING" in msg and "31%" in msg, msg[:70])
    forced_sess = budget_guard.decide(0.13, 0.50, override=True, session_utilization=0.40, session_threshold=0.30)
    check("override does not lift the session line", forced_sess.session_limited
          and budget_guard.gate_record(forced_sess, "pipeline")["decision"] == "SKIP_SESSION")
    ok_sess = budget_guard.decide(0.13, 0.50, session_utilization=0.26, session_threshold=0.30)
    check("26% session proceeds", not ok_sess.session_limited
          and budget_guard.gate_record(ok_sess, "pipeline")["decision"] == "PROCEED")
    check("GATE_STOP_EXIT is 75 (what daily_pipeline.sh matches)", budget_guard.GATE_STOP_EXIT == 75)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
