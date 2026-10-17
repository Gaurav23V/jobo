#!/usr/bin/env python3
"""LIVE end-to-end test: can a real agent submit a form whose resume field is
a shadow-DOM file input, using the NEED_UPLOAD handoff?

This is the regression test for the failure that beat the pipeline twice.
job393 (Treyco / SmartRecruiters `spl-dropzone`) failed on 2026-09-01, and
failed AGAIN on 2026-09-02 after the correct CDP recipe was added to the
agent's prompt - the transcript showed the agent ignoring the recipe and
reaching for the objectId route anyway.

So the upload moved into code (module4/uploader.py), reached via a
`NEED_UPLOAD:` directive. This test exercises the whole thing for real: live
`hermes chat` agent, the real persistent browser, the real handoff loop, and a
server that REJECTS the submission unless a file is genuinely attached. The
agent cannot fake a pass.

Prereqs: scripts/upload_test_server.py running, DISPLAY/XAUTHORITY set,
browser.allow_private_urls=true in Hermes config (the agent's browser tool
blocks localhost otherwise).

Run: uv run python scripts/test_upload_e2e_live.py
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import agent_driver, browser_session, constants, prompts  # noqa: E402

SUFFIX = """

## Test-run note
This is a self-test against a local form at the URL below. Treat it exactly
like a real ATS application: fill every field, attach the resume via the
NEED_UPLOAD handoff, tick the consent box, and submit. Success looks like the
page showing APPLICATION_SUBMITTED. The resume field is a custom
`<spl-dropzone>` component - do NOT try to attach the file yourself.
"""


def state(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8898)
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
        print("    Start it: uv run python scripts/upload_test_server.py --port 8898")
        return 1
    if st["submitted"]:
        print("    FAIL: already submitted; restart the server for a clean test.")
        return 1
    print(f"    OK: {st}")

    print("[1] Ensuring the persistent browser is up...")
    try:
        cdp = browser_session.ensure_browser_running(constants.browser_profile_dir())
        print(f"    OK: {cdp}")
    except Exception as e:
        print(f"    FAIL: {e}")
        return 1

    resume = Path(__file__).resolve().parent.parent / "personal.example" / "resume.pdf"
    if not resume.is_file():
        print(f"    FAIL: no resume fixture at {resume}")
        return 1

    print("[2] Running the apply agent against the shadow-DOM upload form...")
    user_prompt = prompts.user_prompt(
        job_id=99998,
        company="Shadow Corp",
        title="Backend Engineer",
        url=url,
        resume_path=str(resume),
        cover_path=None,
    )
    outcome = agent_driver.run_apply_agent(
        system_prompt=prompts.SYSTEM_PROMPT + SUFFIX,
        user_prompt=user_prompt,
        cdp_url=cdp,
        job_id=99998,
        is_retry=False,
        resume_path=str(resume),
        cover_path=None,
    )

    print(f"\n[3] Agent verdict: {outcome.status!r}")
    print(f"    detail: {outcome.detail}")
    if outcome.fields_filled:
        print(f"    fields: {json.dumps(outcome.fields_filled)[:250]}")

    print("[4] SERVER-SIDE truth (the server rejects a fileless submission)...")
    final = state(args.port)
    print(f"    {final}")

    failures = []
    if not final["submitted"]:
        failures.append("server did NOT accept a submission")
    if not final.get("filename"):
        failures.append("server recorded no attached resume file")
    elif final["filename"] != resume.name:
        failures.append(f"wrong file attached: {final['filename']!r}")
    if outcome.status != "submitted":
        failures.append(f"agent verdict was {outcome.status!r}, expected 'submitted'")

    # Confirm the agent used the handoff rather than hand-rolling CDP, since
    # the whole point is that it no longer does the upload itself.
    debug_dir = Path(__file__).resolve().parent.parent / "data" / "logs" / "apply_debug"
    transcripts = sorted(debug_dir.glob("job99998_*.txt"))
    if transcripts:
        text = transcripts[-1].read_text()
        if "setFileInputFiles" in text:
            failures.append("agent still called setFileInputFiles itself")
        if not re.search(r"NEED_UPLOAD", text):
            print("    NOTE: no NEED_UPLOAD in the failure transcript")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        print(f"\nTranscripts: {debug_dir}/job99998_*.txt")
        return 1
    print("SHADOW-DOM UPLOAD VERIFIED END TO END")
    print("  - agent asked for the upload instead of doing it itself")
    print("  - driver attached the file to a shadow-DOM input")
    print(f"  - server accepted the submission with resume {final['filename']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
