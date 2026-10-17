#!/usr/bin/env python3
"""Test module4.form_filler against the widget shapes that burn turn budgets.

Dropdowns became the top failure once uploads were fixed. On the 2026-09-03
run, 8 of 10 turn-budget failures named a dropdown, each burning the agent's
whole budget on ONE field. A transcript named the cause: "clicked the dropdown
without registering the selection - the underlying hidden `<select id="10955">`
never updated".

The 2026-09-04 rewrite addresses fields by NUMBER instead of prose, because
prose matching produced a SILENT WRONG-FIELD WRITE on a live run: the hint
'field with JSON key "college" (... NOT the "Full name" input)' put the value
into the Full name box and reported ok=True.

Verification is always a DOM read-back of the value the FORM holds, not
whether the CDP call returned cleanly.

Prereq: Chromium with CDP on 9333.
Run: uv run python scripts/test_form_filler.py
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
    """Leave only the probe tab open.

    The persistent browser carries leftover tabs (LinkedIn, a previous ATS),
    and those contain form fields of their own. Without this, a test measures
    whichever page happened to be frontmost rather than the probe.

    Never closes the tab we are keeping, and re-checks the browser afterwards:
    Chromium exits if its last page target goes away, and an over-eager
    cleanup here killed the browser mid-test.
    """
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return
    for t in tabs:
        if t.get("id") != keep_id:
            close_tab(t["id"])
    # Safety net in case the keep tab was already gone.
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
    if not PROBE.is_file():
        print(f"FAIL: no probe page at {PROBE}")
        return 1
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
    except Exception as e:
        print(f"FAIL: no CDP on {CDP}: {e}")
        return 1

    results: list[tuple[str, bool, str]] = []

    def record(label: str, ok: bool, note: str = "") -> None:
        results.append((label, ok, note))
        print(f"  {'PASS' if ok else 'FAIL'}  {label:44s} {note}")

    # ---------------------------------------------------------------
    print("A. Addressing fields by NUMBER (the primary path)")
    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    try:
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        record(
            "list_fields returns numbered controls",
            listing.ok and len(listing.fields) >= 4,
            f"{len(listing.fields)} field(s)",
        )
        by_label = {f.label.lower(): f for f in listing.fields}
        for f in listing.fields:
            print(f"       {f.as_line().strip()}")

        # Native select must expose its options so the agent picks a real one.
        country = next((f for f in listing.fields if "country" in f.label.lower()), None)
        record(
            "native select lists its options",
            bool(country and country.options and "India" in country.options),
            f"options={country.options if country else None}",
        )

        cases = [
            ("Country/Region Code", "India", "country", "in"),
            ("College Name", "LIIT Springfield", "college", "liit-springfield"),
            # Probe field 3 is a server-backed select2: nothing is preloaded,
            # so this only passes if the setter types to trigger the search and
            # then commits a REAL returned result (value, not display text).
            ("School", "Stanford University", "school", "stanford"),
            ("Current CTC", "20 LPA", "notice", "20 LPA"),
            # job489 (Westbay/Rippling): the agent typed 10/04/2026 into a
            # date control, it looked filled, and the form held nothing - the
            # Apply button stayed disabled. A native date input only accepts
            # ISO, so the driver must convert.
            ("Earliest start date", "10/04/2026", "startdate", "2026-10-04"),
            # A plain text field posing as a date has no format of its own to
            # enforce, so the value it holds is whatever was passed. Asserting
            # it gets rewritten to the placeholder's order would be testing a
            # behaviour we do NOT want: silently reformatting a user value in a
            # free-text box risks changing its meaning.
            ("Available from", "2026-11-15", "joindate", "2026-11-15"),
        ]
        for label, value, key, expected in cases:
            fi = next(
                (f for f in listing.fields if label.lower() in f.label.lower()), None
            )
            if not fi:
                record(f"#N set {label}", False, "field not found in listing")
                continue
            res = form_filler.set_field(CDP, f"#{fi.index}", value, index=fi.index)
            time.sleep(0.5)
            got = form_state(tab["webSocketDebuggerUrl"]).get(key, "")
            record(
                f"#{fi.index} set {label}",
                res.ok and got == expected,
                f"stored={got!r} strategy={res.strategy or '-'}",
            )
    finally:
        close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nA2. Never invent an option; substitute an HONEST one instead")
    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    try:
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        idx = {}
        for f in listing.fields:
            lab = f.label.lower()
            if "highest level of education" in lab:
                idx["degree"] = f
            elif "position function" in lab:
                idx["industry"] = f
            elif "no indian colleges" in lab or "school (no" in lab:
                idx["school2"] = f

        # job570: the agent asked for "Bachelor's Degree"; the site only has
        # "Bachelor - Engineering", sitting at position 9 (past the old
        # 8-option display cut-off). The answer book should substitute it.
        f = idx.get("degree")
        if not f:
            record("degree: field found", False, "not in listing")
        else:
            record(
                "long select shows ALL options (not truncated at 8)",
                bool(f.options) and "Bachelor - Engineering" in (f.options or []),
                f"{len(f.options or [])} options exposed",
            )
            res = form_filler.set_field(CDP, f"#{f.index}", "Bachelor's Degree", index=f.index)
            time.sleep(0.5)
            got = form_state(tab["webSocketDebuggerUrl"]).get("degree", "")
            record(
                "degree substitutes to a real option",
                res.ok and got == "beng" and res.substituted == "Bachelor - Engineering",
                f"stored={got!r} substituted={res.substituted!r}",
            )

        # job570 again: "Engineering" is absent, "Information Technology" is
        # present and is true for the candidate.
        f = idx.get("industry")
        if not f:
            record("industry: field found", False, "not in listing")
        else:
            res = form_filler.set_field(CDP, f"#{f.index}", "Engineering", index=f.index)
            time.sleep(0.5)
            got = form_state(tab["webSocketDebuggerUrl"]).get("industry", "")
            record(
                "job function substitutes to a true synonym",
                res.ok and got == "it",
                f"stored={got!r} substituted={res.substituted!r}",
            )

        # THE WINGTIP CASE. the user's college is genuinely not in this list. The old
        # code invented an option and reported success eight times running.
        # It must now fail, hold nothing, and hand back the real option list.
        f = idx.get("school2")
        if not f:
            record("school2: field found", False, "not in listing")
        else:
            res = form_filler.set_field(CDP, f"#{f.index}", "LIIT Springfield", index=f.index)
            time.sleep(0.5)
            got = form_state(tab["webSocketDebuggerUrl"]).get("school2", "")
            record(
                "impossible value FAILS instead of being invented",
                (not res.ok) and got == "",
                f"ok={res.ok} stored={got!r}",
            )
            record(
                "failure reports no_match with the site's real options",
                res.no_match and "Harvard University" in res.available_options,
                f"no_match={res.no_match} opts={len(res.available_options)}",
            )
            msg = res.as_agent_message()
            record(
                "agent message lists the real options and forbids inventing",
                "Harvard University" in msg and "invent" in msg,
                "",
            )
    finally:
        close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nA3. Remote-search widget: search for real, then judge honestly")

    def _school_index() -> int:
        """Number of the remote-search School combobox (probe field 3).

        Addressed by NUMBER on purpose: the probe now has a second control
        whose label also contains "School", so a bare prose hint is genuinely
        ambiguous and (correctly) refused. That is section B's job, not this
        one's.
        """
        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        for fi in listing.fields:
            if "school_input" in (fi.meta or "") or fi.meta.startswith("School | school_input"):
                return fi.index
        for fi in listing.fields:
            if fi.label.strip().lower() == "school":
                return fi.index
        return -1

    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    try:
        # A value the remote "database" DOES have. Nothing is preloaded, so
        # this only works if the setter actually types to trigger the search
        # and then picks a returned result.
        n = _school_index()
        res = form_filler.set_field(CDP, f"#{n}", "Stanford University", index=n)
        time.sleep(0.5)
        got = form_state(tab["webSocketDebuggerUrl"]).get("school", "")
        record(
            "remote search finds and selects a real result",
            res.ok and got == "stanford",
            f"ok={res.ok} stored={got!r} strategy={res.strategy!r}",
        )
    finally:
        close_tab(tab["id"])

    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    try:
        # THE WINGTIP CASE on a remote widget: the search genuinely returns
        # nothing. The old code injected the value here and reported success.
        n = _school_index()
        res = form_filler.set_field(CDP, f"#{n}", "LIIT Springfield", index=n)
        time.sleep(0.5)
        got = form_state(tab["webSocketDebuggerUrl"]).get("school", "")
        record(
            "remote search with NO results is not stuffed with an invention",
            (not res.ok) and got == "",
            f"ok={res.ok} stored={got!r} strategy={res.strategy!r}",
        )
        record(
            "reports empty_remote so the agent hears 'no results'",
            res.no_match and res.empty_remote,
            f"no_match={res.no_match} empty_remote={res.empty_remote}",
        )
        record(
            "no 'injected-option' strategy exists any more",
            "injected" not in (res.strategy or ""),
            f"strategy={res.strategy!r}",
        )
    finally:
        close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nB. THE LIVE BUG: an ambiguous prose hint must REFUSE, not guess")
    # This exact hint wrote "LIIT Springfield" into the Full name box on 2026-09-04
    # while reporting ok=True.
    bad_hints = [
        'field with JSON key "college" (visible label "College Name (required)", '
        'NOT the "Full name" input)',
        'College Name (the field bound to form key "college", currently empty per '
        'debug JSON despite visible text "LIIT Springfield" in the search box)',
    ]
    for i, hint in enumerate(bad_hints):
        tab = new_tab(f"file://{PROBE}")
        time.sleep(1.4)
        close_other_tabs(tab["id"])
        try:
            res = form_filler.set_field(CDP, hint, "LIIT Springfield")
            time.sleep(0.4)
            st = form_state(tab["webSocketDebuggerUrl"])
            wrong_write = st.get("country") == "LIIT Springfield" or (
                st.get("notice") == "LIIT Springfield"
            )
            # Either it resolved to the RIGHT field, or it refused. Never a
            # silent write elsewhere, and never ok=True on the wrong control.
            ok = (res.ok and st.get("college") == "liit-springfield") or (
                not res.ok and not wrong_write
            )
            record(
                f"ambiguous hint {i + 1} refuses or resolves correctly",
                ok,
                f"ok={res.ok} college={st.get('college')!r} matched={res.matched[:28]!r}",
            )
            record(
                f"ambiguous hint {i + 1} never claims success on wrong field",
                not (res.ok and st.get("college") != "liit-springfield"),
                "",
            )
        finally:
            close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nC. Unambiguous prose hints still work (backwards compatibility)")
    fuzzy = [
        ("lowercase hint", "country/region code", "Germany", "country", "de"),
        ("partial hint", "College", "IIT Delhi", "college", "iit-delhi"),
        ("token-overlap hint", "Current CTC (annual)", "24 LPA", "notice", "24 LPA"),
    ]
    for label, hint, value, key, expected in fuzzy:
        tab = new_tab(f"file://{PROBE}")
        time.sleep(1.4)
        close_other_tabs(tab["id"])
        try:
            res = form_filler.set_field(CDP, hint, value)
            time.sleep(0.4)
            got = form_state(tab["webSocketDebuggerUrl"]).get(key, "")
            record(label, res.ok and got == expected, f"stored={got!r}")
        finally:
            close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nD. Failure behaviour")
    tab = new_tab(f"file://{PROBE}")
    time.sleep(1.4)
    close_other_tabs(tab["id"])
    try:
        res = form_filler.set_field(CDP, "Favourite Dinosaur", "Stegosaurus")
        record("unknown field fails cleanly", not res.ok, res.note[:44])

        res2 = form_filler.set_field(CDP, "Country/Region Code", "Atlantis")
        st = form_state(tab["webSocketDebuggerUrl"])
        record(
            "bad option is not stored",
            (not res2.ok) and st.get("country") == "",
            f"country={st.get('country')!r}",
        )

        listing = form_filler.list_fields(CDP)
        form_filler.remember_listing(CDP, listing)
        res3 = form_filler.set_field(CDP, "#99", "x", index=99)
        record("out-of-range number fails cleanly", not res3.ok, res3.note[:44])

        # Stale index: pretend the listing described a different form.
        form_filler._LAST_LISTING[CDP] = ["SELECT~bogus~bogus~gone"] * 10
        res4 = form_filler.set_field(CDP, "#1", "India", index=1)
        record(
            "stale index refuses instead of mis-filling",
            (not res4.ok) and res4.stale,
            res4.note[:44],
        )
        form_filler.remember_listing(CDP, listing)
    finally:
        close_tab(tab["id"])

    # ---------------------------------------------------------------
    print("\nE. Directive parsing")
    parse_cases = [
        ("NEED_FIELD: #2 = LIIT Springfield", ("#2", "LIIT Springfield", 2)),
        ("NEED_FIELD: #12=India", ("#12", "India", 12)),
        ("NEED_FIELD: 3 = India", ("3", "India", 3)),
        ("NEED_FIELD: College Name = LIIT Springfield", ("College Name", "LIIT Springfield", None)),
        ("NEED_FIELD: Years of Java = 2", ("Years of Java", "2", None)),
        ("NEED_FIELD: no equals sign here", None),
        ("something else", None),
    ]
    for line, want in parse_cases:
        got = form_filler.parse_request(line)
        record(f"parse {line[:34]}", got == want, str(got))

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
