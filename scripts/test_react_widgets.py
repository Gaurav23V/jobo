#!/usr/bin/env python3
"""Test the 2026-09-05 fixes against the widget shapes that beat the pipeline.

Three real failures from that run, reproduced:

  1. react-select that REVERTS programmatic values (Wingtip "School", Bluegum
     "School"/"Degree"). Our tool reported success 15+ times while the form
     stayed empty. The fix drives the widget like a human and, critically,
     settles before verifying so a reverted value is reported as a FAILURE.

  2. A Submit button under an invisible overlay (Westbay/Rippling, jobs 487
     and 489). Everything filled, click swallowed, budget gone.

  3. A disabled Submit button - clicking can never work, so the tool must say
     WHY and name the empty required field instead of retrying.

Verification is always the page's own committed state, never "the call
returned ok".

Prereq: Chromium with CDP on 9333.
Run: uv run python scripts/test_react_widgets.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import clicker, form_filler  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "react_widget_probe.html"
CDP = "http://127.0.0.1:9333"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52s} {note}")


def new_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def close_tab(tab_id: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def close_others(keep: str) -> None:
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return
    for t in tabs:
        if t.get("id") != keep:
            close_tab(t["id"])
    try:
        from module4 import browser_session

        browser_session.ensure_guard_tab()
    except Exception:
        pass


def state(ws: str) -> dict:
    with form_filler._Cdp(ws) as c:
        r = c.call(
            "Runtime.evaluate",
            {"expression": "window.__probeState()", "returnByValue": True},
        )
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else {}


def fresh_tab() -> dict:
    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.5)
    close_others(tab["id"])
    return tab


def main() -> int:
    if not PROBE.is_file():
        print(f"FAIL: no probe at {PROBE}")
        return 1
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
    except Exception as e:
        print(f"FAIL: no CDP on {CDP}: {e}")
        return 1

    print("1. react-select that reverts programmatic values")
    tab = fresh_tab()
    try:
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        fi = next((f for f in listing.fields if "school" in f.label.lower()), None)
        check("the react-select is listed as a field", fi is not None,
              f"{len(listing.fields)} field(s)")

        # THE test: does the page's committed state actually hold the value?
        res = form_filler.set_field(CDP, "School", "LIIT Springfield",
                                    index=fi.index if fi else None)
        time.sleep(0.6)
        st = state(tab["webSocketDebuggerUrl"])
        committed = st.get("school", "")
        check(
            "value is COMMITTED in the widget's own state",
            committed == "LIIT Springfield",
            f"committed={committed!r} strategy={res.strategy or '-'}",
        )
        check(
            "reported ok matches reality (no false success)",
            res.ok == (committed == "LIIT Springfield"),
            f"ok={res.ok} committed={committed!r}",
        )
    finally:
        close_tab(tab["id"])

    print("\n2. Honest failure when a value cannot survive")
    # A value with no matching option must NOT be reported as set.
    tab = fresh_tab()
    try:
        res = form_filler.set_field(CDP, "School", "Hogwarts")
        time.sleep(0.5)
        st = state(tab["webSocketDebuggerUrl"])
        check(
            "an impossible option is reported as FAILED",
            not res.ok,
            f"ok={res.ok} note={res.note[:44]}",
        )
        check(
            "and nothing was committed",
            st.get("school", "") == "",
            f"committed={st.get('school')!r}",
        )
    finally:
        close_tab(tab["id"])

    print("\n3. Submit button under an invisible overlay is not bypassed")
    tab = fresh_tab()
    try:
        before = state(tab["webSocketDebuggerUrl"])
        check("not submitted before the click", not before.get("submitted"))
        res = clicker.click(CDP, "Submit application")
        after = state(tab["webSocketDebuggerUrl"])
        check(
            "the overlay-blocked button was NOT clicked",
            after.get("submitted") is False,
            f"strategy={res.strategy or '-'} blocked_by={res.blocked_by or '-'}",
        )
        check("the tool honestly refuses an obscured control", not res.ok, res.note[:80])
        check(
            "it noticed something was covering the button",
            bool(res.blocked_by),
            f"blocked_by={res.blocked_by!r}",
        )
    finally:
        close_tab(tab["id"])

    print("\n4. Bare NEED_CLICK finds an unobstructed submit button by itself")
    tab = fresh_tab()
    try:
        # Fixture setup only: this scenario tests default target selection,
        # not permission to bypass the blocking overlay tested above.
        with clicker._Cdp(tab["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Runtime.evaluate", {"expression": "document.getElementById('overlay').remove()"})
        res = clicker.click(CDP, "")
        time.sleep(0.5)
        after = state(tab["webSocketDebuggerUrl"])
        check("submit found with no label given", after.get("submitted") is True,
              f"matched={res.matched!r}")
    finally:
        close_tab(tab["id"])

    print("\n5. A disabled button explains itself instead of retrying")
    tab = fresh_tab()
    try:
        res = clicker.click(CDP, "Finish")
        check("disabled button is not reported as clicked", not res.ok)
        check("it is identified as disabled", res.disabled, res.note[:44])
        check(
            "the empty required field is named",
            "phone" in res.note.lower() or "req1" in res.note.lower(),
            res.note[:70],
        )
        msg = res.as_agent_message()
        check("agent is told to fix the cause, not re-click",
              "Fix the cause" in msg)
    finally:
        close_tab(tab["id"])

    print("\n6. Directive parsing")
    for line, want in [
        ("NEED_CLICK: Submit application", "Submit application"),
        ("NEED_CLICK", ""),
        ("NEED_CLICK:", ""),
        ("nothing here", None),
    ]:
        got = clicker.parse_request(line)
        check(f"parse {line[:30]!r}", got == want, repr(got))

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
