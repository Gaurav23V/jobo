#!/usr/bin/env python3
"""Offline tests for module4.answer_book.

The answer book exists because the 2026-09-06 run's dominant cost was an agent
guessing phrasings one turn at a time at fields whose option list it could not
fully see. Two properties matter more than coverage:

  1. It never returns something FALSE about the candidate. A wrong-but-accepted
     answer is worse than a blocker: it ships a lie on a real application.
  2. It returns None rather than reaching, so `set_field` reports an honest
     failure instead of inventing an option (the bug this whole change kills).

Run: uv run python scripts/test_answer_book.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import answer_book as ab  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} {note}")


def main() -> int:
    print("A. Picks a TRUE alternative the site actually offers")
    cases = [
        (
            "college spelled out",
            "LIIT Springfield",
            ["Lakeside Institute of Technology Bombay",
             "Lakeside Institute of Information Technology Springfield", "Other"],
            "Lakeside Institute of Information Technology Springfield",
        ),
        (
            "degree phrasing (job570)",
            "Bachelor's Degree",
            ["Associate's Degree", "Advanced Degree - Doctorate",
             "Bachelor - Engineering", "High School"],
            "Bachelor - Engineering",
        ),
        (
            "job function synonym",
            "Engineering",
            ["Board", "Business Development", "Information Technology", "Sales"],
            "Information Technology",
        ),
        (
            "discipline widening",
            "Computer Science",
            ["Computer Science and Engineering", "Mechanical Engineering"],
            "Computer Science and Engineering",
        ),
        (
            "city alias",
            "Springfield City",
            ["Springfield, Illinois, India", "Mumbai, Maharashtra, India"],
            "Springfield, Illinois, India",
        ),
        (
            "yes as single letter",
            "Yes",
            ["Y", "N"],
            "Y",
        ),
        (
            "exact match wins untouched",
            "India",
            ["India", "Indonesia"],
            "India",
        ),
    ]
    for label, value, opts, expected in cases:
        got = ab.pick_from_options(value, opts)
        check(label, got == expected, f"{value!r} -> {got!r}")

    print("\nB. Refuses rather than lying")
    # The Wingtip case: the user's college is genuinely not in their database. Inventing
    # an option here is exactly what shipped a fake school eight times.
    check(
        "no honest option -> None (Wingtip School)",
        ab.pick_from_options("LIIT Springfield", ["Harvard University", "Stanford University", "MIT"])
        is None,
        "",
    )
    check(
        "empty option list -> None",
        ab.pick_from_options("LIIT Springfield", []) is None,
        "",
    )
    check(
        "empty value -> None",
        ab.pick_from_options("", ["Harvard"]) is None,
        "",
    )
    # "IIT Springfield" is a DIFFERENT institution and must never be offered for the candidate.
    check(
        "never substitutes a different institution",
        ab.pick_from_options("LIIT Springfield", ["IIT Springfield", "IIT Delhi"]) is None,
        f"got {ab.pick_from_options('LIIT Springfield', ['IIT Springfield', 'IIT Delhi'])!r}",
    )
    # Short-token containment must not fire: "in" (India) inside "Singapore".
    check(
        "short value does not match by containment",
        ab.pick_from_options("IN", ["Singapore", "Finland"]) is None,
        f"got {ab.pick_from_options('IN', ['Singapore', 'Finland'])!r}",
    )
    # A free-text answer has no chain and nothing sane to guess.
    check(
        "unknown free text -> None",
        ab.pick_from_options("24 LPA", ["Yes", "No"]) is None,
        "",
    )

    print("\nC. Generic escape hatch only as a last resort")
    check(
        "falls back to Other when offered",
        ab.pick_from_options("LIIT Springfield", ["Harvard University", "Other"]) == "Other",
        "",
    )
    check(
        "prefers a real match over Other",
        ab.pick_from_options(
            "LIIT Springfield", ["Lakeside Institute of Information Technology Springfield", "Other"]
        )
        == "Lakeside Institute of Information Technology Springfield",
        "",
    )

    print("\nD. fallbacks_for never echoes the input and stays honest")
    fb = ab.fallbacks_for("LIIT Springfield")
    check("fallbacks exclude the original", "LIIT Springfield" not in fb, f"{len(fb)} alts")
    check(
        "fallbacks never include a different institute",
        not any("iit springfield" == a.lower() for a in fb),
        "",
    )
    check("unknown value yields no fallbacks", ab.fallbacks_for("zxqw") == [], "")

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
