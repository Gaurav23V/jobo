#!/usr/bin/env python3
"""Test module4.outcome_taxonomy.classify_outcome (GHO-2).

Verbatim cases are drawn from the real distribution in data/jobs.db
(2026-10-14, 1668 rows) so this test fails if the classifier ever drifts
from what was actually measured, not just from a plausible-looking guess.

Run: uv run python scripts/test_outcome_taxonomy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4.outcome_taxonomy import OUTCOMES, classify_outcome  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:60s} {note}")


def main() -> int:
    print("1. applied=True always wins, regardless of trailing status text")
    check("plain submitted", classify_outcome(applied=True, last_status="submitted") == "submitted")
    check(
        "applied=True but stale stuck: text (real jobs 162, 185) is still submitted",
        classify_outcome(applied=True, last_status="stuck:Tailspin's Eightfold ATS...") == "submitted",
    )
    check(
        "applied=True with no status text (legacy jobs 19, 200) is still submitted",
        classify_outcome(applied=True, last_status=None) == "submitted",
    )

    print("\n2. applied=False, no status -> never attempted")
    check("NULL status", classify_outcome(applied=False, last_status=None) == "not_attempted")
    check("empty string status", classify_outcome(applied=False, last_status="") == "not_attempted")

    print("\n3. closed listings")
    check("bare 'closed'", classify_outcome(applied=False, last_status="closed") == "closed")
    check(
        "closed:<reason>",
        classify_outcome(
            applied=False, last_status="closed:LinkedIn posting shows 'Not currently accepting'"
        )
        == "closed",
    )

    print("\n4. already_applied counts as submitted (the goal state is true)")
    check(
        "already_applied:<reason>",
        classify_outcome(
            applied=False,
            last_status="already_applied:Osprey's system shows an existing application on file",
        )
        == "submitted",
    )

    print("\n5. policy/refusal outcomes")
    for prefix, example in [
        ("skipped", "skipped:the user chose not to apply (2026-10-04)"),
        ("paused", "paused:ats.rippling.com | apply flow routes through ats.rippling.com"),
        ("linkedin_signed_out", "linkedin_signed_out:Apply flow is gated behind LinkedIn's widget"),
        ("blocked_employer", "blocked_employer:apply flow routes to a blocklisted destination"),
    ]:
        check(f"{prefix}:... -> skipped_by_policy", classify_outcome(applied=False, last_status=example) == "skipped_by_policy")

    print("\n6. genuine failed attempts -> stuck, including prefixes not explicitly listed")
    for example in [
        "stuck:Hit tool-iteration limit mid-form-fill",
        "captcha_failed:hCaptcha presented an image challenge",
        "failed:missing_resume",
        "external_failed:browser died mid-run",
    ]:
        check(f"{example.split(':')[0]}:... -> stuck", classify_outcome(applied=False, last_status=example) == "stuck")

    print("\n7. closed set sanity")
    check(
        "OUTCOMES is exactly the 5 required buckets",
        OUTCOMES == {"submitted", "closed", "stuck", "skipped_by_policy", "not_attempted"},
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
