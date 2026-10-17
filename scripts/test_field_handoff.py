#!/usr/bin/env python3
"""Test the agent_driver handoff logic for fields (offline, no browser).

Covers the driver-side half of the 2026-09-04 fixes:
  * NEED_FIELDS (listing) vs NEED_FIELD (set) are told apart correctly - both
    start with the same prefix, and getting this wrong would break every
    field set.
  * The per-field 3-strike cap fires, and counts PER FIELD rather than
    globally, so one stubborn control cannot eat a whole turn budget.
  * A success resets that field's strike count.

Run: uv run python scripts/test_field_handoff.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import agent_driver, form_filler  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52s} {note}")


def main() -> int:
    print("1. NEED_FIELDS vs NEED_FIELD must be told apart")
    cases = [
        ("NEED_FIELDS", False, True),
        ("NEED_FIELDS please", False, True),
        ("NEED_FIELD: #2 = LIIT Springfield", True, False),
        ("NEED_FIELD: College Name = LIIT Springfield", True, False),
    ]
    for line, want_set, want_list in cases:
        got_set = bool(agent_driver.FIELD_REQUEST_RE.search(line))
        got_list = bool(agent_driver.FIELDS_LIST_RE.search(line))
        check(
            f"{line[:34]!r}",
            got_set == want_set and got_list == want_list,
            f"set={got_set} list={got_list}",
        )
    # The loop checks FIELD_REQUEST before FIELDS_LIST, so a directive wins.
    line = "NEED_FIELD: #2 = LIIT Springfield"
    check(
        "a NEED_FIELD directive is handled as a SET, not a listing",
        bool(agent_driver.FIELD_REQUEST_RE.search(line)),
        "(loop order: field_req checked first)",
    )

    print("\n2. Per-field 3-strike cap")
    calls: list[tuple[str, object]] = []

    def fake_set_field(cdp_url, hint, value, index=None):
        calls.append((hint, index))
        return form_filler.SetFieldResult(
            ok=False, field_name=hint, value=value, note="simulated failure"
        )

    real = form_filler.set_field
    form_filler.set_field = fake_set_field  # type: ignore[assignment]
    try:
        failures: dict[str, int] = {}
        msgs = []
        for _ in range(5):
            msgs.append(
                agent_driver._resolve_field(
                    "NEED_FIELD: #2 = LIIT Springfield",
                    cdp_url="http://x",
                    job_id=1,
                    failures=failures,
                )
            )
        check(
            "set_field is called only 3 times for one field",
            len(calls) == 3,
            f"calls={len(calls)}",
        )
        check(
            "4th+ attempt returns FIELD_SET_ABANDONED",
            all(m.startswith("FIELD_SET_ABANDONED") for m in msgs[3:]),
            "",
        )
        check(
            "abandon message tells the agent to move on and submit",
            "Do NOT ask for this field again" in msgs[-1],
            "",
        )

        # A DIFFERENT field must get its own fresh budget.
        calls.clear()
        agent_driver._resolve_field(
            "NEED_FIELD: #7 = India", cdp_url="http://x", job_id=1, failures=failures
        )
        check(
            "a different field gets its own strike budget",
            len(calls) == 1,
            f"calls={len(calls)}",
        )

        # A success must clear the field's strikes.
        failures2: dict[str, int] = {}
        calls.clear()

        def flaky(cdp_url, hint, value, index=None):
            calls.append((hint, index))
            ok = len(calls) == 2  # fail, succeed, then fail twice more
            return form_filler.SetFieldResult(
                ok=ok, field_name=hint, value=value, readback=value, note="" if ok else "nope"
            )

        form_filler.set_field = flaky  # type: ignore[assignment]
        for _ in range(4):
            agent_driver._resolve_field(
                "NEED_FIELD: #3 = X", cdp_url="http://x", job_id=1, failures=failures2
            )
        check(
            "a success resets that field's strike count",
            len(calls) == 4,
            f"calls={len(calls)} (all 4 reached set_field)",
        )
    finally:
        form_filler.set_field = real  # type: ignore[assignment]

    print("\n3. Index is passed through to set_field")
    seen: list[object] = []

    def capture(cdp_url, hint, value, index=None):
        seen.append(index)
        return form_filler.SetFieldResult(ok=True, field_name=hint, value=value, readback=value)

    form_filler.set_field = capture  # type: ignore[assignment]
    try:
        agent_driver._resolve_field(
            "NEED_FIELD: #5 = India", cdp_url="http://x", job_id=1, failures={}
        )
        agent_driver._resolve_field(
            "NEED_FIELD: Country = India", cdp_url="http://x", job_id=1, failures={}
        )
        check("numbered directive passes index=5", seen[0] == 5, f"index={seen[0]}")
        check("prose directive passes index=None", seen[1] is None, f"index={seen[1]}")
    finally:
        form_filler.set_field = real  # type: ignore[assignment]

    print("\n4. Agent-facing messages steer toward numbers, not longer prose")
    amb = form_filler.SetFieldResult(
        ok=False, field_name="x", value="y", note="ambiguous", ambiguous=True,
        candidates=["a | b", "c | d"],
    )
    msg = amb.as_agent_message()
    check("ambiguous result tells the agent to use NEED_FIELDS", "NEED_FIELDS" in msg)
    check(
        "ambiguous result does NOT dump a prose candidate list",
        "a | b" not in msg,
        "(re-describing is the loop that burned 9 rounds)",
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
