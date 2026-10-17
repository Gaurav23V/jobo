#!/usr/bin/env python3
"""Browser-backed tests for fixes 2 and 3 (2026-09-11 run).

FIX 2 - two controls sharing a label.
  Proseware's Greenhouse form renders the phone dial-code picker with
  id="country" AND a real country-of-residence field. Both listed as
  "Country", the agent addressed the dial-code picker, and burned its whole
  budget pushing "India" into a box that only holds "+91". The listing now
  shows a react-select's COMMITTED value (`currently='+91'`), and a rejected
  value names what the control already holds, so the wrong control is obvious.

FIX 3 - a file attached to a detached node.
  Both Westbay/Rippling jobs failed with "call succeeded but page reports
  files=[(none)]". Measured cause (scripts/diag_upload_stale.py): the form
  re-renders between our DOM walk and the set, so the handle points at an
  orphan. setFileInputFiles succeeds against it and the live field stays
  empty. Verification now checks `isConnected`, and upload_file re-walks once.

Both were confirmed to FAIL before their fix.

Prereq: Chromium with CDP on 9333.
Run: uv run python scripts/test_upload_stale_node.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import browser_session, form_filler, uploader  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
UPLOAD_PROBE = REPO / "scripts" / "upload_stale_probe.html"
COUNTRY_PROBE = REPO / "scripts" / "greenhouse_country_probe.html"
RESUME = REPO / "personal.example" / "resume.pdf"
CDP = "http://127.0.0.1:9333"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} {note}")


def new_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def close_tab(tab_id: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def close_other_tabs(keep_id: str) -> None:
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return
    for t in tabs:
        if t.get("id") != keep_id:
            close_tab(t["id"])
    try:
        browser_session.ensure_guard_tab()
    except Exception:
        pass


def state(ws: str) -> dict:
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

    # ---------------------------------------------------------------
    print("FIX 3: a file on a DETACHED node is not a success")
    tab = new_tab(f"file://{UPLOAD_PROBE}")
    time.sleep(1.7)   # past the probe's re-render
    close_other_tabs(tab["id"])
    ws = tab["webSocketDebuggerUrl"]
    try:
        r = uploader.upload_file(CDP, str(RESUME), want="resume")
        live = state(ws).get("input-resume") or []
        check(
            "THE BUG: upload reports ok only when the LIVE field holds the file",
            r.ok == bool(live),
            f"ok={r.ok} live={live}",
        )
        check(
            "the file really is in the live document",
            "resume.pdf" in live,
            f"live={live}",
        )
    finally:
        close_tab(tab["id"])

    # A stale handle must be REPORTED as detached, not silently passed.
    tab = new_tab(f"file://{UPLOAD_PROBE}")
    time.sleep(0.3)   # BEFORE the swap
    close_other_tabs(tab["id"])
    ws = tab["webSocketDebuggerUrl"]
    try:
        targets = uploader.find_file_inputs(CDP)
        stale = next((t for t in targets if "resume" in t.descriptor), None)
        time.sleep(1.5)   # let the swap strand it
        if stale is None:
            check("found a resume input to strand", False, "")
        else:
            with uploader._Cdp(ws) as cdp:
                cdp.call(
                    "DOM.setFileInputFiles",
                    {
                        "files": [str(RESUME.resolve())],
                        "backendNodeId": stale.backend_node_id,
                    },
                )
                ok, seen = uploader._verify_attached(
                    cdp, RESUME.name, stale.backend_node_id
                )
            check(
                "THE BUG: verification rejects a file on a detached node",
                not ok,
                f"ok={ok} seen={seen[:60]!r}",
            )
            check(
                "and says WHY, so the log names the mechanism",
                "DETACHED" in seen,
                f"seen={seen[:60]!r}",
            )
            live = state(ws).get("input-resume") or []
            check(
                "the live field really was still empty",
                not live,
                f"live={live}",
            )
    finally:
        close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nFIX 2: two controls sharing a label are distinguishable")
    tab = new_tab(f"file://{COUNTRY_PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    ws = tab["webSocketDebuggerUrl"]
    try:
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        prefix = next((f for f in listing.fields if f.meta.strip() == "country"), None)
        real = next((f for f in listing.fields if "country_residence" in f.meta), None)

        check(
            "both country-ish controls are listed",
            prefix is not None and real is not None,
            f"prefix={prefix.index if prefix else None} real={real.index if real else None}",
        )
        if prefix:
            check(
                "THE BUG: the dial-code picker shows what it already holds",
                "+91" in (prefix.value or ""),
                f"currently={prefix.value!r}",
            )
            check(
                "so the agent's numbered list makes the wrong one obvious",
                "+91" in prefix.as_line(),
                "",
            )
            # Setting India on the prefix must fail AND explain why.
            res = form_filler.set_field(
                CDP, f"#{prefix.index}", "India", index=prefix.index
            )
            check(
                "setting a country on the dial-code picker fails",
                not res.ok,
                f"ok={res.ok}",
            )
            check(
                "the failure names the held value and points at the other control",
                "+91" in (res.note or "") and "WRONG control" in (res.note or ""),
                f"note={(res.note or '')[-90:]!r}",
            )
        if real:
            res2 = form_filler.set_field(
                CDP, f"#{real.index}", "India", index=real.index
            )
            time.sleep(0.5)
            st = state(ws)
            check(
                "the REAL country field still accepts India",
                res2.ok and st.get("country_residence") == "India",
                f"ok={res2.ok} stored={st.get('country_residence')!r}",
            )
            check(
                "and the dial code was left untouched",
                st.get("phone_prefix") == "+91",
                f"prefix={st.get('phone_prefix')!r}",
            )
    finally:
        close_tab(tab["id"])

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
