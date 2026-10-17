#!/usr/bin/env python
"""Account setup no longer eats the form's step budget (GHO-20).

1464 Willowby (Workday, 2026-10-15) spent its first rounds on "Sign in with
email" clicks and a password reset, then ran out at 80/80 on My Information,
step 1 of 5. 1622 Kestrel (Avature) asked for two resets that never mailed:
there was no account, and the form itself creates one.

Covers:
  * the first sign-in / create-account / reset handoff adds ACCOUNT_SETUP_STEPS
    and ACCOUNT_SETUP_SEC once, and the agent is told its new total
  * ordinary clicks (Submit, Next) add nothing
  * the out-of-budget detail keeps the BASE budget number, so blockers.py
    still recognises an escalated attempt that ran out
  * a reset with no mail steers the agent to creating an account

Offline: no browser, no Gmail, no model calls, no subprocesses.
"""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4 import agent_driver, blockers, password_reset, runner  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def drive(outputs: list[str], counts: list[int], *, budget_boost: bool = False):
    """Run the driver against scripted agent rounds; return (outcome, calls)."""
    calls = []
    outs = iter(outputs)
    steps = iter(counts)

    def fake_run(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append({"max_turns": int(max_turns), "timeout": timeout_sec, "prompt": prompt})
        return next(outs) + "\n\nsession_id: s1\n", False

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", side_effect=lambda sid, p: next(steps)))
        st.enter_context(patch.object(agent_driver, "_resolve_click", return_value="CLICK_DONE: page changed"))
        st.enter_context(patch.object(agent_driver, "_resolve_password_reset", return_value="PASSWORD_RESET_LINK: x"))
        st.enter_context(patch.object(agent_driver, "_resolve_field", return_value="FIELD_SET: #1"))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": ""}))
        out = agent_driver.run_apply_agent(
            system_prompt="S", user_prompt="U", cdp_url="http://x", job_id=1464,
            resume_path="/r.pdf", is_retry=budget_boost, budget_boost=budget_boost,
        )
    return out, calls


def test_reset_and_signin_grant_once():
    print("\n[1] sign-in click + reset: one allowance, agent told the new total")
    base = agent_driver.AGENT_STEPS_FIRST
    extra = agent_driver.ACCOUNT_SETUP_STEPS
    # 1464's shape: two sign-in clicks, a reset, then form filling until the cap.
    out, calls = drive(
        [
            "NEED_CLICK: Sign in with email",
            "NEED_CLICK: Sign in with email",
            "NEED_PASSWORD_RESET: willowby.wd1.myworkdayjobs.com",
            "NEED_FIELD: #2 = LinkedIn",
            "NEED_FIELD: #15 = Illinois",
        ],
        [10, 18, 25, 70, base + extra],
    )
    check("round 2 gets the base budget plus the allowance",
          calls[1]["max_turns"] == base + extra - 10, str(calls[1]["max_turns"]))
    check("the second sign-in click adds nothing",
          calls[2]["max_turns"] == base + extra - 18, str(calls[2]["max_turns"]))
    check("the reset adds nothing more",
          calls[3]["max_turns"] == base + extra - 25, str(calls[3]["max_turns"]))
    check("time is extended too",
          calls[1]["timeout"] > agent_driver.AGENT_TIMEOUT_FIRST_SEC, str(calls[1]["timeout"]))
    check("the agent is told its new step budget", f"now {base + extra}" in calls[1]["prompt"],
          calls[1]["prompt"])
    check("only the first grant is announced", "now " not in calls[2]["prompt"])
    check("form rounds continue past the old 80-step cap", len(calls) == 5, f"{len(calls)} rounds")
    check("detail keeps the base budget number", f"({base} model steps)" in out.detail, out.detail)
    check("detail names the account allowance", f"plus {extra} for account setup" in out.detail, out.detail)
    check("runner still reads it as budget exhaustion", runner._is_budget_exhaustion(f"stuck:{out.detail}"))


def test_ordinary_clicks_grant_nothing():
    print("\n[2] Submit / Next clicks add nothing")
    base = agent_driver.AGENT_STEPS_FIRST
    out, calls = drive(
        ["NEED_CLICK: Next", "NEED_CLICK: Submit Application", "NEED_FIELD: #1 = x"],
        [30, 60, base],
    )
    check("budget unchanged after Next", calls[1]["max_turns"] == base - 30, str(calls[1]["max_turns"]))
    check("budget unchanged after Submit", calls[2]["max_turns"] == base - 60, str(calls[2]["max_turns"]))
    check("no allowance note", not any("Account setup" in c["prompt"] for c in calls))
    check("detail has no allowance suffix", "account setup" not in out.detail, out.detail)
    for label in ("Create Account", "Forgot your password?", "Sign In", "Log in", "Register",
                  "Verify your email", "Sign up"):
        check(f"{label!r} counts as account setup",
              bool(agent_driver._ACCOUNT_SETUP_CLICK.search(f"NEED_CLICK: {label}")))
    for label in ("Submit", "Next", "Save and Continue", "Apply Manually", "Review", "Upload"):
        check(f"{label!r} does not", not agent_driver._ACCOUNT_SETUP_CLICK.search(f"NEED_CLICK: {label}"))


def test_escalated_still_permanent():
    print("\n[3] escalated attempt that runs out (with allowance) is still recognised")
    esc = agent_driver.AGENT_STEPS_ESCALATED
    extra = agent_driver.ACCOUNT_SETUP_STEPS
    out, _ = drive(
        ["NEED_CLICK: Create Account", "NEED_FIELD: #1 = x"],
        [20, esc + extra],
        budget_boost=True,
    )
    check("detail carries the escalated base number", f"({esc} model steps)" in out.detail, out.detail)
    check("blockers' escalated-exhausted pattern still matches",
          bool(blockers._ESCALATED_BUDGET_STILL_EXHAUSTED.search(out.detail)), out.detail)


def test_reset_without_mail_steers_to_new_account():
    print("\n[4] no reset mail: agent is told to create an account, not reset again")
    miss = password_reset.ResetLinkResult(
        error="no password-reset email for careerhub.kestrel.com in the last 15 minutes"
    )
    with patch.object(agent_driver.time, "sleep"), \
            patch.object(password_reset, "find_reset_link", return_value=miss), \
            patch("module4.linkedin_session.is_protected_account_domain", return_value=False):
        msg = agent_driver._resolve_password_reset(
            "NEED_PASSWORD_RESET: careerhub.kestrel.com", job_id=1622
        )
    check("still reports the failure", msg.startswith("PASSWORD_RESET_FAILED:"), msg[:80])
    check("explains no account is the likely cause", "no account" in msg, msg)
    check("tells it not to reset again", "do not request another reset" in msg, msg)
    check("gives the standard password for the new account", password_reset.STANDARD_PASSWORD in msg, msg)


def main() -> int:
    test_reset_and_signin_grant_once()
    test_ordinary_clicks_grant_nothing()
    test_escalated_still_permanent()
    test_reset_without_mail_steers_to_new_account()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
