#!/usr/bin/env python3
"""Does a stale node handle explain the Rippling upload failure?

On the 2026-09-11 run both Westbay jobs (Rippling ATS) failed with:

    Upload failed: tried 2 file input(s), none accepted the file.
    data-testid="input-resume" accept=".doc,.docx,.pdf: call succeeded but
    page reports files=[(none)]

"Call succeeded, page holds nothing" has an obvious candidate explanation - the
form re-rendered and replaced the input, so our backendNodeId pointed at a
detached node. This script TESTS that rather than assuming it, because the
last upload theory that got written up as fact ("handles go stale between CDP
calls") was disproven by experiment and wasted a rewrite.

Measures three things against a probe that deliberately swaps its inputs:
  1. baseline  - walk, wait for the swap, then set. Should FAIL if the theory
                 holds, and the failure should look exactly like the live one.
  2. re-walk   - walk again immediately before setting. Should SUCCEED.
  3. verify    - confirm via the LIVE document, not the handle we set, so a
                 stale-handle success cannot fool the check.

Run: uv run python scripts/diag_upload_stale.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import browser_session, uploader  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "upload_stale_probe.html"
RESUME = REPO / "personal.example" / "resume.pdf"
CDP = "http://127.0.0.1:9333"


def new_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def close_tab(tab_id: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def live_state(ws: str) -> dict:
    with uploader._Cdp(ws) as c:
        r = c.call(
            "Runtime.evaluate",
            {"expression": "window.__formState()", "returnByValue": True},
        )
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else {}


def main() -> int:
    browser_session.ensure_healthy(Path.home() / "jobo-linkedin-profile")
    browser_session.ensure_guard_tab()

    if not RESUME.is_file():
        print(f"FAIL: no resume pdf at {RESUME}")
        return 1

    print("=== 1. BASELINE: capture the handle, let the form re-render, then set ===")
    tab = new_tab(f"file://{PROBE}")
    time.sleep(0.3)
    ws = tab["webSocketDebuggerUrl"]
    try:
        # Walk BEFORE the swap fires (it is scheduled at 900ms).
        targets = uploader.find_file_inputs(CDP)
        print(f"   found {len(targets)} file input(s) pre-swap")
        stale_id = targets[0].backend_node_id if targets else None

        time.sleep(1.4)  # let the re-render replace the nodes
        st = live_state(ws)
        print(f"   swaps so far: {st.get('swaps')}")

        if stale_id is not None:
            with uploader._Cdp(ws) as cdp:
                res = cdp.call(
                    "DOM.setFileInputFiles",
                    {"files": [str(RESUME.resolve())], "backendNodeId": stale_id},
                )
                err = res.get("error", {}).get("message") if res.get("error") else None
            print(f"   setFileInputFiles on the STALE handle -> error={err!r}")
            after = live_state(ws)
            print(f"   live document now holds: {after.get('input-resume')}")
            if not after.get("input-resume"):
                print("   => CONFIRMED: the call reports no error and the page holds nothing.")
            else:
                print("   => theory NOT confirmed: the stale handle still reached the live input.")
    finally:
        close_tab(tab["id"])

    print("\n=== 2. FIX: re-walk immediately before setting ===")
    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.6)  # past the swap
    ws = tab["webSocketDebuggerUrl"]
    try:
        r = uploader.upload_file(CDP, str(RESUME), want="resume")
        print(f"   upload_file -> ok={r.ok} detail={r.detail[:110]}")
        after = live_state(ws)
        print(f"   live document holds: {after.get('input-resume')}")
        ok = bool(after.get("input-resume"))
        print(f"   => {'PASS' if ok else 'FAIL'}: file present in the LIVE document")
    finally:
        close_tab(tab["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
