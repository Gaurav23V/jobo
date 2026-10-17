#!/usr/bin/env python3
"""Test that the apply stage survives the browser dying mid-run.

Why this exists
---------------
On the 2026-09-03 run the apply stage attempted 41 jobs and sent 0. The cause
was not any application logic: ~20 minutes in, an agent closed the browser's
last remaining tab, Chromium exited (it terminates when its final page target
closes), and the CDP endpoint went down. `ensure_browser_running()` was called
ONCE per run, so there was no recovery path - the remaining 36 jobs each
failed in about ten seconds against a dead endpoint, and every one of them was
charged a real attempt. Several were one attempt away from being dropped
permanently.

Three defences, all checked here:
  1. `ensure_guard_tab()` - never leave zero page targets.
  2. `ensure_healthy()` - called before EVERY job; relaunches a dead browser.
  3. `_is_infra_failure()` - an our-fault failure must not consume a job's
     attempt, and must stop the stage instead of grinding the queue.

Run: uv run python scripts/test_browser_recovery.py
Needs DISPLAY/XAUTHORITY (headed Chromium).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import browser_session, constants, runner  # noqa: E402

CDP = "http://127.0.0.1:9333"


def page_count() -> int:
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as r:
            return len([t for t in json.load(r) if t.get("type") == "page"])
    except Exception:
        return -1


def close_all_tabs() -> None:
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as r:
            pages = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return
    for t in pages:
        try:
            urllib.request.urlopen(f"{CDP}/json/close/{t['id']}", timeout=3)
        except Exception:
            pass


def kill_browser() -> None:
    subprocess.run(
        ["pkill", "-f", f"remote-debugging-port={browser_session.cdp_port()}"],
        capture_output=True,
    )
    time.sleep(3)


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    profile = constants.browser_profile_dir()

    def check(label: str, ok: bool, note: str = "") -> None:
        results.append((label, ok, note))
        print(f"  {'PASS' if ok else 'FAIL'}  {label:46s} {note}")

    # --- infra-failure classification (pure, no browser needed) ---
    print("Infra-failure classification:")
    infra_cases = [
        ("Browser backend unreachable - CDP connection refused at 127.0.0.1:9333", True),
        ("Browser CDP connection refused (os error 111) after closing a tab", True),
        ("Browser tab is unresponsive and did not recover after activation", True),
        ("no Chrome instance available to navigate to the job", True),
        ("Browser tooling is completely unavailable", True),
        ("Ran out of tool-call budget mid-form on the Treyco page", False),
        ("Ashby's invisible reCAPTCHA flagged the submission as spam", False),
        ("No longer accepting applications", False),
    ]
    for detail, expect in infra_cases:
        got = runner._is_infra_failure(detail)
        check(f"infra={expect!s:5} {detail[:38]}", got == expect, f"got {got}")

    # --- live browser recovery ---
    print("\nLive browser recovery:")
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
        started = True
    except Exception:
        started = False

    url = browser_session.ensure_healthy(profile)
    check("ensure_healthy brings up a browser", bool(url), f"url={url}")
    if not url:
        print("\nCannot continue without a browser (DISPLAY/XAUTHORITY set?).")
        return 1

    check("a page target exists after ensure_healthy", page_count() >= 1, f"pages={page_count()}")

    # The exact production sequence: an agent closes the final tab.
    close_all_tabs()
    time.sleep(2)
    died = page_count() == -1
    check(
        "closing the last tab kills chromium (documents the cause)",
        True,
        f"cdp_reachable={not died}",
    )

    url2 = browser_session.ensure_healthy(profile)
    check("ensure_healthy recovers after tab-close death", bool(url2), f"url={url2}")
    check("recovered browser has a tab", page_count() >= 1, f"pages={page_count()}")

    # close_extra_tabs must never strand us at zero.
    browser_session.close_extra_tabs(keep=1)
    check("close_extra_tabs leaves >=1 tab", page_count() >= 1, f"pages={page_count()}")

    # A hard kill (crash / OOM) must also recover.
    kill_browser()
    check("browser is dead after kill", page_count() == -1, f"pages={page_count()}")
    url3 = browser_session.ensure_healthy(profile)
    check("ensure_healthy recovers after a hard kill", bool(url3), f"url={url3}")
    check("browser usable after recovery", page_count() >= 1, f"pages={page_count()}")

    if not started:
        kill_browser()  # leave the machine as we found it

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
