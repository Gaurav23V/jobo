#!/usr/bin/env python3
"""Replay the ACTUAL directives from the 2026-09-06 failures.

The suite is not enough on its own. `test_form_filler` passed 15/15 throughout
the entire wrong-field era because the probe's labels were tidier than a real
ATS's. So before believing this fix, replay the exact sequences the failing
jobs produced and assert the LOOP no longer happens.

The three real sequences, from data/logs/apply_debug/:

  job570 (Wingtip/Avature)   #19 = Bachelor's Degree  -> failed
                          #19 = Bachelor - Engineering
                          #35 = Engineering        -> failed
                          #35 = Information Technology
                          #20 = LIIT Springfield          x8, each reported SET,
                                                   each a lie

  job516 (Greenhouse)     School = LIIT Springfield
                          School = Lakeside Institute of Information Technology Springfield
                          School = Lakeside Institute of Technology

What we assert is not "it works" but "the second guess is unnecessary":
the FIRST directive of each pair must now succeed on its own, and the
impossible one must fail honestly without storing anything.

Prereq: Chromium with CDP on 9333.
Run: uv run python scripts/test_live_failure_replay.py
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
PROBE = REPO / "scripts" / "dropdown_probe.html"
CDP = "http://127.0.0.1:9333"

results: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, note: str = "") -> None:
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
        from module4 import browser_session

        browser_session.ensure_guard_tab()
    except Exception:
        pass


def form_state(ws_url: str) -> dict:
    with form_filler._Cdp(ws_url) as c:
        r = c.call(
            "Runtime.evaluate",
            {"expression": "window.__formState()", "returnByValue": True},
        )
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else {}


def main() -> int:
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
    except Exception as e:
        print(f"FAIL: no CDP on {CDP}: {e}")
        return 1

    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    ws = tab["webSocketDebuggerUrl"]
    try:
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        by = {}
        for f in listing.fields:
            lab = f.label.lower()
            if "highest level of education" in lab:
                by["degree"] = f
            elif "position function" in lab:
                by["industry"] = f
            elif "no indian colleges" in lab:
                by["school2"] = f

        print("job570 replay: the guess-then-correct loop must not be needed")
        # Directive 1 of the real pair. On 2026-09-06 this FAILED and the agent
        # spent another turn on "Bachelor - Engineering".
        f = by["degree"]
        r1 = form_filler.set_field(CDP, f"#{f.index}", "Bachelor's Degree", index=f.index)
        time.sleep(0.4)
        got = form_state(ws).get("degree", "")
        record(
            "first degree directive succeeds (no 2nd turn needed)",
            r1.ok and got == "beng",
            f"stored={got!r} substituted={r1.substituted!r}",
        )

        f = by["industry"]
        r2 = form_filler.set_field(CDP, f"#{f.index}", "Engineering", index=f.index)
        time.sleep(0.4)
        got = form_state(ws).get("industry", "")
        record(
            "first job-function directive succeeds (no 2nd turn needed)",
            r2.ok and got == "it",
            f"stored={got!r} substituted={r2.substituted!r}",
        )

        print("\njob570/job516 replay: the impossible college must not loop")
        # The real killer: eight identical directives, each answered
        # "Field SET", none of them true. Send the same directive repeatedly
        # and assert every single one fails and stores nothing.
        f = by["school2"]
        oks, stored = [], []
        for _ in range(8):
            r = form_filler.set_field(CDP, f"#{f.index}", "LIIT Springfield", index=f.index)
            time.sleep(0.25)
            oks.append(r.ok)
            stored.append(form_state(ws).get("school2", ""))
        record(
            "impossible college never reports success (x8)",
            not any(oks),
            f"ok flags={oks}",
        )
        record(
            "impossible college never stores a value (x8)",
            all(s == "" for s in stored),
            f"distinct stored={sorted(set(stored))}",
        )
        last = form_filler.set_field(CDP, f"#{f.index}", "LIIT Springfield", index=f.index)
        msg = last.as_agent_message()
        record(
            "agent is handed the real option list to choose from",
            "Harvard University" in msg and "Stanford University" in msg,
            "",
        )
        record(
            "agent is told not to retry other spellings",
            "spelling" in msg.lower(),
            "",
        )

        print("\njob516 replay: the three-spelling hunt now ends at guess 1")
        # All three spellings the agent tried must fail identically - none of
        # them is in the list, so none may ever be reported as set.
        spellings = [
            "LIIT Springfield",
            "Lakeside Institute of Information Technology Springfield",
            "Lakeside Institute of Technology",
        ]
        outs = []
        for s in spellings:
            r = form_filler.set_field(CDP, f"#{f.index}", s, index=f.index)
            time.sleep(0.25)
            outs.append((s, r.ok, form_state(ws).get("school2", "")))
        record(
            "every spelling fails honestly, none is invented",
            all((not ok) and stored_v == "" for _, ok, stored_v in outs),
            f"{[(s[:18], ok) for s, ok, _ in outs]}",
        )
        print("\njob516 replay: a failed search must leave NO residue")
        # Bluegum/Greenhouse. The agent reported "School, Degree and Discipline
        # share state - typing into one clears the others" and we shipped a
        # cleanup fix for it. Be precise about what is actually proven here:
        #
        #   PROVEN: our setter used to leave its failed query sitting in the
        #   combobox. That is plainly wrong on any form - it is uncommitted
        #   text the page never asked for, it can be submitted verbatim, and a
        #   re-render can read it as pending state. The assertions below cover
        #   exactly that, and they failed before the fix.
        #
        #   NOT PROVEN: that Greenhouse specifically wipes SIBLING fields
        #   because of it. That is the agent's explanation, not a measurement,
        #   and an attempt to reproduce the sibling wipe against a faithful
        #   react-select clone did not show it. Do not assert it here and do
        #   not repeat it as fact - if it recurs, capture the live DOM instead
        #   of trusting the transcript.
        listing2 = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing2)
        gh = {}
        for fi in listing2.fields:
            if "gh_degree" in (fi.meta or ""):
                gh["degree"] = fi
            elif "gh_school" in (fi.meta or ""):
                gh["school"] = fi

        if len(gh) < 2:
            record("greenhouse trio: fields found", False, f"found {list(gh)}")
        else:
            rd = form_filler.set_field(
                CDP, f"#{gh['degree'].index}", "Bachelor's Degree", index=gh["degree"].index
            )
            time.sleep(0.4)
            record(
                "react combobox commits a real option",
                rd.ok and form_state(ws).get("gh_degree", "") == "Bachelor's Degree",
                f"degree={form_state(ws).get('gh_degree')!r}",
            )

            rs = form_filler.set_field(
                CDP, f"#{gh['school'].index}", "LIIT Springfield", index=gh["school"].index
            )
            time.sleep(0.6)
            st = form_state(ws)
            record(
                "failed search reports failure",
                not rs.ok,
                f"ok={rs.ok}",
            )
            record(
                "THE FIX: failed search leaves NO typed residue behind",
                st.get("gh_school", "") == "",
                f"school box={st.get('gh_school')!r}",
            )
            record(
                "a committed sibling is untouched by the failed search",
                st.get("gh_degree", "") == "Bachelor's Degree",
                f"degree={st.get('gh_degree')!r}",
            )

            # Cleanup must not break the widget: a later real search still works.
            r2 = form_filler.set_field(
                CDP, f"#{gh['school'].index}", "Stanford University", index=gh["school"].index
            )
            time.sleep(0.6)
            record(
                "widget still usable after a cleaned-up failure",
                r2.ok and form_state(ws).get("gh_school", "") == "Stanford University",
                f"school={form_state(ws).get('gh_school')!r}",
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
