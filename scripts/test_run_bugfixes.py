#!/usr/bin/env python3
"""Tests for the four fixes from the 2026-09-11 full-queue run.

Each test is written to FAIL without its fix - verified by reverting each one
in turn. A test that passes either way proves nothing, which is how an earlier
suite sat at 15/15 through an entire wrong-field era.

Covered here (offline only - the browser-backed halves live in
test_upload_stale_node.py and test_form_filler.py):

  1. a browser fault must not be charged to the job
  4. the prompt must forbid falling back to hand-clicking

Run: uv run python scripts/test_run_bugfixes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import prompts, runner  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:60s} {note}")


# The verbatim detail job611 (Apple) produced on 2026-09-11. It matched no
# marker, so the browser's failure was charged to the job.
JOB611 = (
    "Browser session unresponsive at CDP layer, navigate/snapshot/vision all "
    "timed out repeatedly before the page could even load — infrastructure "
    "issue, not a form/site blocker."
)


def main() -> int:
    print("FIX 1: a browser fault is OUR fault, never the job's")
    check(
        "THE BUG: job611's real wording is recognised as infra",
        runner._is_infra_failure(JOB611),
        "",
    )
    for label, detail in [
        ("browser session crashed", "The browser session crashed mid-form."),
        ("became unresponsive", "The browser became unresponsive and stopped answering."),
        ("lost connection", "Browser backend lost connection while navigating."),
        ("disconnected", "Browser disconnected before the form loaded."),
        ("legacy: cdp refused", "CDP connection refused at 127.0.0.1:9333"),
        ("legacy: os error", "os error 111 talking to the browser"),
        # The one that got away on 2026-09-13, one run after a literal for the
        # near-identical wording was added. A slash was enough to defeat it,
        # which is why matching is now shape-based.
        (
            "slashed wording (job611, run 2)",
            "Browser/CDP session unresponsive, navigate and snapshot both "
            "timing out repeatedly before the page even loaded",
        ),
        ("chromium crashed", "Chromium crashed mid-form"),
        ("devtools unreachable", "the devtools endpoint is unreachable"),
    ]:
        check(f"infra recognised: {label}", runner._is_infra_failure(detail), "")

    print("\n  ...but a SITE failure must still be the job's own result")
    for label, detail in [
        ("captcha", "Ashby's invisible reCAPTCHA flagged the submission."),
        ("dead listing", "LinkedIn posting shows 'Not currently accepting applications'."),
        (
            "site slowness mentioning the browser",
            "The careers site was slow to load in the browser but the form "
            "rejected the application because School had no valid option.",
        ),
        (
            "oauth wall",
            "Portal only offers Continue with Google, no email/password option.",
        ),
    ]:
        check(f"NOT infra: {label}", not runner._is_infra_failure(detail), "")

    print("\n  ...and the queue-protection script must agree with the runner")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "reset_attempts", Path(__file__).resolve().parent / "reset_attempts.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    classify = getattr(mod, "classify", None)
    if classify is None:
        check("reset_attempts exposes a classifier", False, "no classify()")
    else:
        # NOTE: the two answer DIFFERENT questions and must not be conflated.
        # `_is_infra_failure` asks "was this our browser breaking?" (skip the
        # attempt charge). `classify` asks "is this retryable at all?" - a
        # broader set that includes budget exhaustion. So every infra failure
        # must classify as 'ours', but not every 'ours' is an infra failure.
        for detail in [
            JOB611,
            "Browser/CDP session unresponsive, navigate and snapshot timing out",
            "Browser instance became unresponsive and then unreachable",
        ]:
            check(
                f"infra -> 'ours': {detail[:38]}",
                classify(f"stuck:{detail}") == "ours",
                f"got {classify(f'stuck:{detail}')!r}",
            )
        check(
            "a genuinely blocked job is NOT handed an attempt back",
            classify("stuck:Ashby's invisible reCAPTCHA flagged the submission")
            == "blocked",
            "",
        )

    print("\nFIX 4: the prompt forbids hand-clicking after a FIELD_SET_FAILED")
    sp = prompts.SYSTEM_PROMPT
    check(
        "THE BUG: prompt explicitly forbids the manual fallback",
        "Do NOT fall back to hand-clicking" in sp,
        "",
    )
    check(
        "prompt explains a failure is information, not a dead end",
        "INFORMATION, not a dead end" in sp,
        "",
    )
    check(
        "prompt gives a next directive for each failure kind",
        "print NEED_FIELDS" in sp and "wrong control" in sp,
        "",
    )
    check(
        "prompt no longer tells the agent to try react widgets by hand first",
        "do not try them by hand first" in sp,
        "",
    )
    check(
        "prompt warns that two fields can share a label",
        "already holds" in sp and "+91" in sp,
        "",
    )

    print("\nFIX 5 (found by the 6-job batch): NEED_CLICK is the DEFAULT")
    # All four jobs that reached a form died at the final click - an unticked
    # consent box, a submit that did nothing - and every one reported click=0
    # handoffs. The prompt framed NEED_CLICK as a fallback for buttons that
    # "won't click", so agents hand-fought checkboxes until the budget ran out
    # and never reached the fallback.
    check(
        "THE BUG: NEED_CLICK is framed as the default, not a fallback",
        "DEFAULT for clicking" in sp,
        "",
    )
    check(
        "prompt names the consent-checkbox failure explicitly",
        "consent checkbox" in sp.lower(),
        "",
    )
    check(
        "prompt says first time, not third",
        "first time, not" in sp,
        "",
    )
    check(
        "the dropdown section no longer contradicts it with 'try ONCE by hand'",
        "try ONCE by hand" not in sp,
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
