#!/usr/bin/env python
"""A model-provider outage is OUR failure, not the job's.

Found on the 2026-09-15 run: the model provider was unreachable for ~6 minutes
at 03:17. The agent subprocess died before printing a STATUS line, so:

  1. parse_verdict() fell through to its "stuck" default with an EMPTY detail,
     which persisted as a bare `stuck:` - no reason in the DB row and none in
     the audit receipt. The reason existed only in the debug transcript.
  2. runner._is_infra_failure() saw that empty string, said "not infra", and
     charged each job a real attempt. 18 consecutive jobs (Google, Merlin,
     Eastlake, Alpineer among them) each lost one of their three attempts for an
     outage on our side.

Offline: no browser, no model calls, no DB writes.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4.agent_driver import parse_verdict  # noqa: E402
from module4.runner import _is_infra_failure  # noqa: E402
from scripts.reset_attempts import classify  # noqa: E402

PASS = 0
FAIL = 0

# Verbatim tail of data/logs/apply_debug/job722_20260915_032223.txt (Google).
REAL_OUTAGE_OUTPUT = (
    "API call failed after 3 retries: Hermes can't reach the model provider. "
    "You may be offline. Check your internet connection and try again.\n\n\n"
    "session_id: 20260915_032201_200c2b\n"
)

# Wordings the same fault can arrive in. The agent does not write these - the
# harness does - but the provider's own message text is not ours to control.
OUTAGE_VARIANTS = (
    REAL_OUTAGE_OUTPUT,
    "API call failed after 5 retries: cannot reach the model provider.",
    "Hermes can't reach the model provider. You may be offline.",
    "",  # process died producing nothing at all
)

# Genuine site failures. None of these may be mistaken for an outage, or a real
# dead end would retry forever and never exhaust its attempts.
SITE_FAILURES = (
    "Lever form fully completed but hCaptcha served a drag-and-drop puzzle.",
    "Brightloop RippleHire form requires Date of birth and no DOB is available.",
    "jobs.apple.com requires Apple ID sign-in before the application form.",
    "Greenhouse react-select fields silently reject programmatic values.",
    "Ran out of tool-call iterations mid-form-fill; application not submitted.",
    "The posting shows 'No longer accepting applications'.",
)


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def test_no_verdict_still_explains_itself():
    print("\n[1] an agent that dies without a verdict still records WHY")
    outcome = parse_verdict(REAL_OUTAGE_OUTPUT)
    check("status is stuck", outcome.status == "stuck", f"got {outcome.status!r}")
    check(
        "detail is NOT empty (the bare `stuck:` bug)",
        bool(outcome.detail.strip()),
        f"got {outcome.detail!r}",
    )
    check(
        "detail names the provider failure",
        "model provider" in outcome.detail.lower(),
        f"got {outcome.detail[:120]!r}",
    )
    empty = parse_verdict("")
    check(
        "totally empty output still explains itself",
        bool(empty.detail.strip()),
        f"got {empty.detail!r}",
    )


def test_outage_is_classified_as_infra():
    print("\n[2] the runner does not charge an attempt for an outage")
    for raw in OUTAGE_VARIANTS:
        detail = parse_verdict(raw).detail
        label = (raw[:46] + "...") if raw else "<no output at all>"
        check(f"infra: {label}", _is_infra_failure(detail), f"detail={detail[:90]!r}")


def test_site_failures_are_not_infra():
    print("\n[3] real site failures are still the job's problem")
    for detail in SITE_FAILURES:
        check(
            f"not infra: {detail[:52]}...",
            not _is_infra_failure(detail),
            "wrongly treated as our infrastructure",
        )


def test_verdict_parsing_is_unharmed():
    print("\n[4] normal verdicts are untouched")
    ok = parse_verdict("STATUS: submitted\nDETAIL: Greenhouse confirmation page shown.")
    check("submitted still parses", ok.status == "submitted")
    check("its detail survives", "Greenhouse" in ok.detail)

    stuck = parse_verdict("STATUS: stuck\nDETAIL: hCaptcha blocked the submit button.")
    check("explicit stuck still parses", stuck.status == "stuck")
    check(
        "an explicit stuck keeps ITS OWN detail",
        stuck.detail == "hCaptcha blocked the submit button.",
        f"got {stuck.detail!r}",
    )
    check(
        "an explicit stuck is not called infra",
        not _is_infra_failure(stuck.detail),
    )

    closed = parse_verdict("STATUS: closed\nDETAIL: No longer accepting applications.")
    check("closed still parses", closed.status == "closed")

    garbage = parse_verdict("the agent rambled and never printed a status line")
    check("unparseable output is stuck", garbage.status == "stuck")
    check("unparseable output explains itself", bool(garbage.detail.strip()))


def test_reset_classifier_agrees():
    print("\n[5] reset_attempts hands these attempts back (lists must not drift)")
    for raw in OUTAGE_VARIANTS:
        detail = parse_verdict(raw).detail
        verdict = classify(f"stuck:{detail}")
        check(
            f"our fault: {(raw[:40] + '...') if raw else '<no output>'}",
            verdict == "ours",
            f"classified as {verdict!r}",
        )
    for detail in SITE_FAILURES[:3]:
        verdict = classify(f"stuck:{detail}")
        check(
            f"site failure not auto-reset: {detail[:40]}...",
            verdict != "ours",
            f"classified as {verdict!r}",
        )


def main() -> int:
    print("model-provider outage accounting regressions")
    test_no_verdict_still_explains_itself()
    test_outage_is_classified_as_infra()
    test_site_failures_are_not_infra()
    test_verdict_parsing_is_unharmed()
    test_reset_classifier_agrees()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
