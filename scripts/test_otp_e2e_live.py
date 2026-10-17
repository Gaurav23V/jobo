#!/usr/bin/env python3
"""LIVE end-to-end test of the Module 4 OTP handoff.

This is the real thing, not a mock: a live `hermes chat` browser agent, the
real persistent Chromium instance, a real code delivered to the user's real Gmail
inbox, and the actual run_apply_agent() handoff loop. The only synthetic piece
is the ATS itself (scripts/otp_test_server.py), because no real employer will
issue verification codes on demand.

Passes only if the agent gets all the way to APPLICATION_SUBMITTED, which is
impossible without correctly retrieving and entering the emailed code.

Prereqs: scripts/otp_test_server.py running on --port, DISPLAY/XAUTHORITY set.
Run: uv run python scripts/test_otp_e2e_live.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import agent_driver, browser_session, constants, prompts  # noqa: E402

SYSTEM_PROMPT_SUFFIX = """

## Test-run note
This is a self-test against a local form at the URL given below. Treat it
exactly like a real ATS application: fill the fields, trigger the emailed
verification code, use the NEED_OTP handoff to obtain it, enter it, and submit.
Success looks like the page showing APPLICATION_SUBMITTED.
"""


def server_state(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    url = f"http://127.0.0.1:{args.port}/"

    print("[0] Checking the test ATS is up...")
    try:
        state = server_state(args.port)
    except Exception as e:
        print(f"    FAIL: test server not reachable on port {args.port}: {e}")
        print("    Start it with: uv run python scripts/otp_test_server.py")
        return 1
    if state["submitted"]:
        print("    FAIL: server already has submitted=True; restart it for a clean test.")
        return 1
    print(f"    OK: server up, state={state}")

    print("[1] Ensuring the persistent browser is running...")
    try:
        cdp = browser_session.ensure_browser_running(constants.browser_profile_dir())
        print(f"    OK: CDP at {cdp}")
    except Exception as e:
        print(f"    FAIL: {e}")
        return 1

    print("[2] Running the apply agent against the OTP-gated form...")
    print("    (this spawns a real agent; expect a few minutes)")

    resume = Path(__file__).resolve().parent.parent / "personal.example" / "resume.pdf"
    user_prompt = prompts.user_prompt(
        job_id=99999,
        company="Selftest Corp",
        title="Backend Engineer",
        url=url,
        resume_path=str(resume),
        cover_path=None,
    )

    outcome = agent_driver.run_apply_agent(
        system_prompt=prompts.SYSTEM_PROMPT + SYSTEM_PROMPT_SUFFIX,
        user_prompt=user_prompt,
        cdp_url=cdp,
        job_id=99999,
        is_retry=False,
    )

    print(f"\n[3] Agent verdict: status={outcome.status!r}")
    print(f"    detail: {outcome.detail}")
    if outcome.fields_filled:
        print(f"    fields: {json.dumps(outcome.fields_filled)[:300]}")

    print("[4] Verifying against SERVER-SIDE truth (not the agent's self-report)...")
    final = server_state(args.port)
    print(f"    server state: {final}")

    failures = []
    if not final["sent"]:
        failures.append("server never sent a code - the agent didn't trigger the OTP flow")
    if not final["submitted"]:
        failures.append(
            "server did NOT record a submission - the agent never entered a valid code"
        )
    if outcome.status != "submitted":
        failures.append(f"agent verdict was {outcome.status!r}, expected 'submitted'")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        print("\nCheck data/logs/apply_debug/job99999_*.txt for the agent transcript.")
        return 1
    print("LIVE OTP HANDOFF VERIFIED END TO END")
    print("  - agent triggered the emailed code")
    print("  - driver retrieved it from the real Gmail inbox")
    print("  - agent resumed the same browser session and entered it")
    print("  - server confirmed APPLICATION_SUBMITTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
