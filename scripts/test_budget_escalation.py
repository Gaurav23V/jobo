#!/usr/bin/env python3
"""Guard the retry-budget escalation against upstream truncation.

The escalation decides whether a retry gets 30min/95 turns (because the job
merely ran out of room) or 8min/40 (because it hit a structural wall). It
decides by looking for a phrase inside the stored failure detail - so ANY
truncation upstream of the store silently flips the decision.

This has now bitten twice:
  - `persist.record_failed_attempt` cut to 150 chars (fixed, widened to 600)
  - `agent_driver._parse_outcome` cut to 300 chars (fixed 2026-09-11)

The second one made the first fix useless: the string arrived already short.
job516 filled an ENTIRE Greenhouse form, ran out of turns before Submit, and
its retry was handed the SHORT budget because the deciding phrase sat at
character ~310 of its verdict.

These are offline string tests on purpose - no browser, no network - so they
run everywhere and cannot be skipped when the display is unavailable.

Run: uv run python scripts/test_budget_escalation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import persist, runner  # noqa: E402
from module4.agent_driver import parse_verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:56s} {note}")


# The REAL verdict job516 emitted on 2026-09-11. The deciding phrase "ran out
# of tool-call budget" sits past character 300.
JOB516_DETAIL = (
    "Form was fully filled (name, email, phone, country=India, resume "
    "uploaded, School=LIIT Springfield/Degree=Bachelor of Technology/Discipline="
    "Computer Science all verified holding via JS-native-setter after the "
    "known react-select shared-state bug wiped Discipline once, dates, "
    "LinkedIn, backend experience=1, distributed systems experience=1, all "
    "confirmed persisted) but I ran out of tool-call budget before verifying "
    'the final "Have you read the JD" yes/no field and locating/clicking the '
    "Submit button. No submission was made."
)

JOB516_TRANSCRIPT = f"STATUS: stuck\nDETAIL: {JOB516_DETAIL}\n"


def main() -> int:
    print("A. The deciding phrase survives parsing")
    outcome = parse_verdict(JOB516_TRANSCRIPT)
    marker_at = JOB516_DETAIL.lower().find("ran out of")
    check(
        "the marker really is past the old 300-char cut",
        marker_at > 300,
        f"marker at char {marker_at}",
    )
    check(
        "parsed detail is not truncated",
        outcome.detail == JOB516_DETAIL,
        f"parsed {len(outcome.detail)} of {len(JOB516_DETAIL)} chars",
    )
    check(
        "parsed status is correct",
        outcome.status == "stuck",
        f"status={outcome.status!r}",
    )

    print("\nB. The escalation actually fires end to end")
    stored = f"{outcome.status}:{outcome.detail[:600]}"   # what persist writes
    check(
        "stored status still contains the marker",
        "ran out of" in stored.lower(),
        f"{len(stored)} chars stored",
    )
    check(
        "THE BUG: _is_budget_exhaustion fires on job516's real verdict",
        runner._is_budget_exhaustion(stored),
        "",
    )

    print("\nC. Regression: the old truncation would have broken it")
    old_style = f"{outcome.status}:{JOB516_DETAIL[:300]}"
    check(
        "a 300-char cut loses the marker (why this test exists)",
        not runner._is_budget_exhaustion(old_style),
        "confirmed the old behaviour was broken",
    )

    print("\nD. Structural failures must NOT be escalated")
    for label, detail in [
        ("captcha wall", "Ashby's invisible reCAPTCHA flagged the submission."),
        (
            "oauth-only wall",
            "Portal only offers Continue with Google, no email/password option.",
        ),
        (
            "impossible dropdown",
            "Wingtip's School autocomplete has no entry for LIIT Springfield under any "
            "spelling tried; required field blocks progression.",
        ),
    ]:
        check(
            f"no escalation for {label}",
            not runner._is_budget_exhaustion(f"stuck:{detail}"),
            "",
        )

    print("\nE. persist's store is wide enough for a real verdict")
    check(
        "persist keeps at least 600 chars",
        "[:600]" in Path(persist.__file__).read_text(),
        "",
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
