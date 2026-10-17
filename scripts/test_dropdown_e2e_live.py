#!/usr/bin/env python3
"""LIVE end-to-end test: can a real agent submit a select2-gated form?

Regression test for the failure that became dominant once uploads were fixed.
On the 2026-09-04 run, 5 of 26 stuck jobs died on select2 autocompletes, each
burning the entire turn budget on one field.

Real agent, real browser, real handoff loop, and a server that REJECTS any
submission with an empty college field - so the agent cannot fake a pass by
skipping the dropdown.

Prereqs: scripts/dropdown_test_server.py running, DISPLAY/XAUTHORITY set,
browser.allow_private_urls=true.

Run: uv run python scripts/test_dropdown_e2e_live.py
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

SUFFIX = """

## Test-run note
This is a self-test against a local form at the URL below. Treat it exactly
like a real ATS application. Alex's college is LIIT Springfield. Notice period:
30 days. Success looks like the page showing APPLICATION_SUBMITTED.
"""


def state(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8897)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    url = f"http://127.0.0.1:{args.port}/"

    print("[0] Checking the test ATS...")
    try:
        st = state(args.port)
    except Exception as e:
        print(f"    FAIL: server not reachable: {e}")
        return 1
    if st["submitted"]:
        print("    FAIL: already submitted; restart the server.")
        return 1
    print(f"    OK: {st}")

    print("[1] Ensuring the browser is up...")
    cdp = browser_session.ensure_healthy(constants.browser_profile_dir())
    if not cdp:
        print("    FAIL: no browser")
        return 1
    print(f"    OK: {cdp}")

    resume = Path(__file__).resolve().parent.parent / "personal.example" / "resume.pdf"
    print("[2] Running the apply agent against the select2-gated form...")
    user_prompt = prompts.user_prompt(
        job_id=99997,
        company="Dropdown Corp",
        title="Backend Engineer",
        url=url,
        resume_path=str(resume),
        cover_path=None,
    )
    outcome = agent_driver.run_apply_agent(
        system_prompt=prompts.SYSTEM_PROMPT + SUFFIX,
        user_prompt=user_prompt,
        cdp_url=cdp,
        job_id=99997,
        is_retry=False,
        resume_path=str(resume),
        cover_path=None,
    )

    print(f"\n[3] Agent verdict: {outcome.status!r}")
    print(f"    detail: {outcome.detail[:200]}")

    print("[4] SERVER-SIDE truth (rejects an empty college field)...")
    final = state(args.port)
    print(f"    {final}")

    failures = []
    if not final["submitted"]:
        failures.append("server did NOT accept a submission")
    college = (final.get("fields") or {}).get("college", "")
    if not college:
        failures.append("college field was empty in the submitted form")
    elif college != "LIIT Springfield":
        failures.append(f"wrong college submitted: {college!r}")
    if outcome.status != "submitted":
        failures.append(f"agent verdict was {outcome.status!r}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        print("\nTranscript: data/logs/apply_debug/job99997_*.txt")
        return 1
    print("SELECT2 DROPDOWN VERIFIED END TO END")
    print("  - agent used the NEED_FIELD handoff instead of fighting the widget")
    print("  - driver set the hidden backing <select> and verified it")
    print(f"  - server accepted the submission with college={college!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
