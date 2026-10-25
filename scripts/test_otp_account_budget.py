#!/usr/bin/env python
"""A delivered emailed code or link gets the account-setup allowance (GHO-96).

FedEx 2244 (Paradox chat, 2026-10-25) spent most of its 80 steps on the chat
intake, asked for the emailed code, got it, and ran out at 80/80 two digits
into typing it, before the application form behind the verification wall.
Sign-in clicks and password resets already added ACCOUNT_SETUP_STEPS once; an
emailed code did not.

Covers:
  * a delivered code adds ACCOUNT_SETUP_STEPS and ACCOUNT_SETUP_SEC once, and
    the agent is told its new total in the same prompt as the code
  * a lookup that finds nothing, or a code already given, adds nothing
  * a reset followed by a code still adds the allowance only once, so the
    total stays bounded
  * the out-of-budget detail keeps the BASE budget number

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

from module4 import agent_driver, runner  # noqa: E402

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


def drive(outputs: list[str], counts: list[int], codes: list[tuple[str | None, str]]):
    """Run the driver against scripted agent rounds; return (outcome, calls)."""
    calls = []
    outs = iter(outputs)
    steps = iter(counts)
    otp = iter(codes)

    def fake_run(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append({"max_turns": int(max_turns), "timeout": timeout_sec, "prompt": prompt})
        return next(outs) + "\n\nsession_id: s1\n", False

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", side_effect=lambda sid, p: next(steps)))
        st.enter_context(patch.object(agent_driver, "_resolve_otp", side_effect=lambda *a, **k: next(otp)))
        st.enter_context(patch.object(agent_driver, "_resolve_password_reset", return_value="PASSWORD_RESET_LINK: x"))
        st.enter_context(patch.object(agent_driver, "_resolve_field", return_value="FIELD_SET: #1"))
        st.enter_context(patch.object(agent_driver, "_seen_workday", return_value=False))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": ""}))
        out = agent_driver.run_apply_agent(
            system_prompt="S", user_prompt="U", cdp_url="http://x", job_id=2244,
            resume_path="/r.pdf",
        )
    return out, calls


def test_code_after_long_chat_grants_once():
    print("\n[1] 2244's shape: code arrives late, budget grows once")
    base = agent_driver.AGENT_STEPS_FIRST
    extra = agent_driver.ACCOUNT_SETUP_STEPS
    out, calls = drive(
        ["NEED_OTP: fedex.paradox.ai", "NEED_OTP: fedex.paradox.ai", "NEED_FIELD: #3 = x"],
        [74, 79, base + extra],
        [("85251", "from Olivia"), ("40711", "from Olivia")],
    )
    check("the code is handed over", "VERIFICATION_CODE: 85251" in calls[1]["prompt"], calls[1]["prompt"])
    check("the round after the code gets the allowance",
          calls[1]["max_turns"] == base + extra - 74, str(calls[1]["max_turns"]))
    check("the agent is told its new step budget", f"now {base + extra}" in calls[1]["prompt"],
          calls[1]["prompt"])
    check("time is extended too",
          calls[1]["timeout"] > agent_driver.AGENT_TIMEOUT_FIRST_SEC, str(calls[1]["timeout"]))
    check("a second code adds nothing more",
          calls[2]["max_turns"] == base + extra - 79, str(calls[2]["max_turns"]))
    check("only the first grant is announced", "Account setup" not in calls[2]["prompt"], calls[2]["prompt"])
    check("detail keeps the base budget number", f"({base} model steps)" in out.detail, out.detail)
    check("detail names the account allowance", f"plus {extra} for account setup" in out.detail, out.detail)
    check("runner still reads it as budget exhaustion", runner._is_budget_exhaustion(f"stuck:{out.detail}"))


def test_no_code_grants_nothing():
    print("\n[2] no code found, or the same code again: nothing added")
    base = agent_driver.AGENT_STEPS_FIRST
    out, calls = drive(
        ["NEED_OTP: example.paradox.ai", "NEED_OTP: example.paradox.ai", "NEED_FIELD: #1 = x"],
        [40, 50, base],
        [(None, "no matching mail in the last 10 minutes"), (None, "no matching mail")],
    )
    check("budget unchanged after a miss", calls[1]["max_turns"] == base - 40, str(calls[1]["max_turns"]))
    check("budget unchanged after a second miss", calls[2]["max_turns"] == base - 50, str(calls[2]["max_turns"]))
    check("no allowance note", not any("Account setup" in c["prompt"] for c in calls))
    check("detail has no allowance suffix", "account setup" not in out.detail, out.detail)

    out, calls = drive(
        ["NEED_OTP: example.paradox.ai", "NEED_OTP: example.paradox.ai", "NEED_FIELD: #1 = x"],
        [40, 50, base + agent_driver.ACCOUNT_SETUP_STEPS],
        [("111222", "ok"), ("111222", "ok")],
    )
    check("a repeated code is not handed over again", "VERIFICATION_CODE" not in calls[2]["prompt"],
          calls[2]["prompt"])


def test_reset_then_code_bounded():
    print("\n[3] reset then code: still one allowance")
    base = agent_driver.AGENT_STEPS_FIRST
    extra = agent_driver.ACCOUNT_SETUP_STEPS
    _, calls = drive(
        ["NEED_PASSWORD_RESET: example.myportal.com", "NEED_OTP: example.myportal.com", "NEED_FIELD: #1 = x"],
        [20, 30, base + extra],
        [("909090", "ok")],
    )
    check("reset grants the allowance", calls[1]["max_turns"] == base + extra - 20, str(calls[1]["max_turns"]))
    check("the code after it adds nothing", calls[2]["max_turns"] == base + extra - 30, str(calls[2]["max_turns"]))


def main() -> int:
    test_code_after_long_chat_grants_once()
    test_no_code_grants_nothing()
    test_reset_then_code_bounded()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
