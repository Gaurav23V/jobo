#!/usr/bin/env python3
"""Reproduce Greenhouse's phone-prefix vs Country ambiguity (job591).

On the 2026-09-11 run, Proseware's Greenhouse form failed with:

    Field set failed (1/3 for #4): the value did not survive the form
    re-rendering (field now "+91")

The instinct is "our readback grabbed a neighbour's value". This probe exists
to test that claim rather than assume it, because Greenhouse renders TWO
react-selects that both legitimately answer to "Country":

  * the phone-number country PREFIX widget, whose input carries id="country"
    and whose committed value is "+91"
  * the actual Country-of-residence field

If `list_fields` labels the phone prefix as "Country", then the agent asking
for `#N = India` is addressing the WRONG CONTROL, and the "+91" readback is
our code correctly reporting that India never committed there. That is a
field-identification bug, not a readback bug, and the fix is different.

Run: uv run python scripts/diag_greenhouse_country.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import form_filler  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "greenhouse_country_probe.html"
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


def close_other_tabs(keep_id: str) -> None:
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return
    for t in tabs:
        if t.get("id") != keep_id:
            close_tab(t["id"])


def main() -> int:
    # Chromium exits when its last page target closes, and this probe closes
    # its own tab on the way out - so a second run finds a dead endpoint.
    # Bring it back rather than making the caller do it.
    try:
        from module4 import browser_session

        browser_session.ensure_healthy(Path.home() / "jobo-linkedin-profile")
        browser_session.ensure_guard_tab()
    except Exception as e:
        print(f"note: could not ensure the browser is up ({e})")

    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    ws = tab["webSocketDebuggerUrl"]
    try:
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        print("=== what the agent is shown ===")
        for f in listing.fields:
            print(f"  #{f.index}  label={f.label!r}")
            print(f"        meta={f.meta!r}")

        country_like = [
            f for f in listing.fields if "country" in (f.label or "").lower()
        ]
        print(f"\ncontrols whose label mentions 'country': {len(country_like)}")
        for f in country_like:
            print(f"  #{f.index} {f.label!r}  meta={f.meta!r}")

        print("\n=== set the FIRST country-ish control to India ===")
        if country_like:
            t = country_like[0]
            r = form_filler.set_field(CDP, f"#{t.index}", "India", index=t.index)
            print(f"  ok={r.ok} readback={r.readback!r} note={(r.note or '')[:110]}")

        with form_filler._Cdp(ws) as c:
            st = c.call(
                "Runtime.evaluate",
                {"expression": "window.__formState()", "returnByValue": True},
            )
            print("\nform state:", st["result"]["result"]["value"])
    finally:
        close_tab(tab["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
