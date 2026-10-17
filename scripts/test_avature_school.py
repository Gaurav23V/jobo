#!/usr/bin/env python3
"""Wingtip/Avature education block: remote select2 School + native month dates.

Live failure (overnight run 2026-10-03, jobs 818 and 936, both dropped after
3 attempts): NEED_FIELD on Wingtip's required "School" answered "search returned
no results" in ~0 ms without sending any search, and "11/2022" / "06/2026"
into the Start/Graduation date fields "did not survive the form re-rendering".

Measured mechanisms (on the real careers.wingtip.com form):
  * School is select2 over an EMPTY <select>, fed by an ajax catalogue with a
    1000 ms debounce, and select2 is registered on a private jQuery
    (window.avajQuery), not window.jQuery. The old code saw zero options and
    stopped. The catalogue has no LIIT Springfield, but does have "(School not
    listed)", which the form's own help text asks for.
  * Dates are <input type="month">; the browser blanks any value that is not
    "yyyy-mm", so "11/2022" became "".

Runs against scripts/avature_school_probe.html (local replica, same select2
4.0.12 build Wingtip serves, local transport instead of the network).
Self-isolated: own headless Chromium on a random port, never 9333.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import form_filler as f  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("avature_school_probe.html")


def main() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)

    with tempfile.TemporaryDirectory(prefix="jobo-avature-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + profile,
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = Path(profile) / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            with sync_playwright() as p:
                page = p.chromium.connect_over_cdp(cdp).contexts[0].pages[0]

                def load():
                    page.goto(PROBE.as_uri())
                    page.wait_for_function("() => !!window.avajQuery && !!avajQuery('#6363-8-0').data('select2')")

                def listing():
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    return ls

                def put(label, value):
                    ls = listing()
                    fi = next((x for x in ls.fields if x.label.lower().startswith(label.lower())), None)
                    if not fi:
                        return f.SetFieldResult(False, note="not listed")
                    return f.set_field(cdp, "#" + str(fi.index), value, index=fi.index)

                sel = lambda id_: page.evaluate(
                    "(i)=>{const s=document.getElementById(i);return [s.value,(s.options[s.selectedIndex]||{}).text||'']}", id_)
                val = lambda id_: page.evaluate("(i)=>document.getElementById(i).value", id_)
                valid = lambda id_: page.evaluate("(i)=>fieldValid(i)", id_)
                is_open = lambda: page.evaluate("()=>!!document.querySelector('.select2-container--open')")

                load()
                # The probe really is Wingtip's shape: select2 is NOT on window.jQuery.
                check("probe: window.jQuery has no select2 (private avajQuery copy)",
                      page.evaluate("()=>!window.jQuery.fn.select2 && !!avajQuery.fn.select2"))

                # 1. Listing hides the template row and lists School once.
                ls = listing()
                labels = [x.label for x in ls.fields]
                check("listing: hidden data-samplerow template controls are not listed",
                      not any("sample" in (x.meta or "") for x in ls.fields), str(labels))
                check("listing: School appears exactly once",
                      sum(1 for x in ls.fields if x.label.lower().startswith("school")) == 1, str(labels))

                # 2. LIIT Springfield is not in the catalogue -> the form's own escape option.
                r = put("School", "LIIT Springfield")
                check("School=LIIT Springfield: reported ok", r.ok, r.note)
                check("School=LIIT Springfield: real search was sent to the catalogue",
                      "liit springfield" in page.evaluate("()=>searchLog"), str(page.evaluate("()=>searchLog")))
                check("School=LIIT Springfield: committed '(School not listed)' id 4932",
                      sel("6363-8-0") == ["4932", "(School not listed)"], str(sel("6363-8-0")))
                check("School=LIIT Springfield: answer tells the agent it was substituted",
                      r.substituted == "(School not listed)" and "had no 'LIIT Springfield'" in r.as_agent_message(),
                      r.as_agent_message())
                check("School: rendered select2 text shows the choice",
                      "(School not listed)" in page.evaluate("()=>document.getElementById('select2-6363-8-0-container').innerText"))
                check("School: dropdown closed afterwards", not is_open())
                check("School: form validator accepts it", valid("6363-8-0"))

                # 3. Never a different school / another field's escape.
                load()
                r = put("School", "Lakeside Institute of Information Technology Springfield")
                check("School=LIIT Springfield (long form): never picks LIIT Allahabad / Company Not Listed",
                      sel("6363-8-0")[1] == "(School not listed)", str(sel("6363-8-0")))

                # 4. An exact catalogue entry is selected directly.
                load()
                r = put("School", "Malaviya National Institute of Technology, Riverton")
                check("School=exact catalogue entry: ok, no substitution",
                      r.ok and not r.substituted and sel("6363-8-0")[0] == "4289", f"{r.note} {sel('6363-8-0')}")
                check("School exact: strategy is select2-remote-search", r.strategy == "select2-remote-search", r.strategy)

                # 5. A remote field with NO escape option fails honestly and cleanly.
                r = put("Major", "Computer Science")
                check("Major (no escape): fails honestly", not r.ok and r.no_match, r.note)
                check("Major (no escape): note says the catalogue has no not-listed option",
                      'no "not listed"/"Other" option' in r.note, r.note)
                check("Major (no escape): nothing committed, dropdown closed",
                      sel("major")[0] == "" and not is_open(), str(sel("major")))

                # 6. Native month inputs.
                load()
                for label, typed, want, id_ in [
                    ("Start Date", "11/2022", "2022-11", "6363-36-0"),
                    ("Graduation", "06/2026", "2026-06", "6363-3-0"),
                ]:
                    r = put(label, typed)
                    check(f"{label}={typed!r}: stored {want!r} and accepted",
                          r.ok and val(id_) == want and valid(id_), f"{r.note} value={val(id_)!r}")
                for typed, want in [("Jun 2026", "2026-06"), ("2026-06", "2026-06"), ("November 2022", "2022-11")]:
                    r = put("Graduation", typed)
                    check(f"month accepts {typed!r} -> {want!r}", r.ok and val("6363-3-0") == want,
                          f"{r.note} value={val('6363-3-0')!r}")
                r = put("Graduation", "soon")
                check("month refuses an unreadable value with a yyyy-mm hint",
                      not r.ok and "yyyy-mm" in r.note, r.note)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
