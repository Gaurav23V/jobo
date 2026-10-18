#!/usr/bin/env python3
"""Ashby School/Location search typeaheads: never a false SET, never another school.

Live failure (run 2026-10-18 22:00, job1923, GHO-57): the candidate's school
is not in Ashby's school catalogue, which has no "Other"/"not listed" entry
either. NEED_FIELD School answered FIELD_SET 'Other' "verified in the DOM",
and Location the same, but Submit then found both fields empty. The agent
went on to type spellings by hand, landed on a different campus, cleared it,
and ran out of steps.

Measured mechanisms (on the real jobs.ashbyhq.com form):
  * The option finder read every `[class*="-option"]` on the page, so the
    always-visible Degree/Gender radio choices ("Other") were offered as
    School results; the setter clicked a different field's radio label.
  * The readback took the input's own value, i.e. the query it had just typed,
    as the committed value. Ashby wipes uncommitted text on blur.
  * The answer book's escapes only ran against an option list, and a typeahead
    only lists results for the query typed, so aliases were never searched.

Runs against scripts/ashby_school_probe.html (local replica of those shapes).
Self-isolated: own headless Chromium on a random port, never 9333.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import answer_book  # noqa: E402
from module4 import form_filler as f  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("ashby_school_probe.html")


def main() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)

    school = answer_book.fallbacks_for("LIIT Springfield")
    check("personal.example school chain is loaded (aliases then escapes)",
          "Lakeside Institute of Information Technology, Springfield" in school and "Other" in school,
          str(school))

    with tempfile.TemporaryDirectory(prefix="jobo-ashby-", ignore_cleanup_errors=True) as profile:
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

                def load(cat):
                    page.goto(PROBE.as_uri() + "?cat=" + cat)
                    page.wait_for_function("() => !!window.committed_school")

                def put(label, value):
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    fi = next((x for x in ls.fields if x.label.lower().startswith(label.lower())), None)
                    if not fi:
                        return f.SetFieldResult(False, note="not listed: " + str([x.label for x in ls.fields]))
                    return f.set_field(cdp, "#" + str(fi.index), value, index=fi.index)

                committed = lambda kind: page.evaluate(f"() => committed_{kind}()")
                shown = lambda ph: page.evaluate(
                    "(p) => document.querySelector(`input[placeholder=\"${p}\"]`).value", ph)
                radios = lambda: page.evaluate(
                    "() => [...document.querySelectorAll('input[type=radio]')].filter(r => r.checked)"
                    ".map(r => r.name + '=' + r.value)")
                searched = lambda: page.evaluate("() => searchLog")

                # 1. No true school and no escape in the catalogue (job1923).
                load("plain")
                t0 = time.monotonic()
                r = put("School", "LIIT Springfield")
                took = time.monotonic() - t0
                check("plain: School is NOT reported set", not r.ok, r.as_agent_message()[:200])
                check("plain: nothing committed, input left empty",
                      committed("school") == "" and shown("Search schools...") == "",
                      f"{committed('school')!r} / {shown('Search schools...')!r}")
                check("plain: no radio of another field was clicked", radios() == [], str(radios()))
                log = searched()
                check("plain: every alias was searched in the catalogue",
                      all(f"school:{a}" in log for a in school[:5]), str(log))
                check("plain: the not-listed / Other escapes were searched too",
                      "school:School not listed" in log and "school:Other" in log, str(log))
                check("plain: result says typeahead and lists what it searched",
                      r.typeahead and r.searched and r.searched[-1] == school[-1], str(r.searched))
                msg = r.as_agent_message()
                check("plain: agent is told not to type more spellings",
                      "Do NOT type more spellings" in msg and "DIFFERENT" in msg, msg[:300])
                check("plain: walk stays inside its time cap", took < f.TYPEAHEAD_WALK_SEC + 15, f"{took:.1f}s")

                # 2. Location with no true answer must not borrow a radio's "Other".
                load("plain")
                r = put("Location", "Shelbyville, Nowhere")
                check("location: unknown place is NOT reported set", not r.ok, r.as_agent_message()[:200])
                check("location: no radio of another field was clicked", radios() == [], str(radios()))
                check("location: substitution never picked 'Other'", r.substituted != "Other", r.substituted)

                # 3. The catalogue holds one of the candidate's other true names.
                load("alias")
                r = put("School", "LIIT Springfield")
                want = "Lakeside Institute of Information Technology, Springfield"
                check("alias: School set via the alias found by searching", r.ok and r.substituted == want,
                      f"ok={r.ok} sub={r.substituted!r} note={r.note!r}")
                check("alias: the exact alias is committed (survives blur)",
                      committed("school") == want and shown("Search schools...") == want,
                      repr(committed("school")))
                check("alias: no radio of another field was clicked", radios() == [], str(radios()))

                # 4. Asking for the generic name never picks another campus.
                load("plain")
                r = put("School", "Lakeside Institute of Information Technology")
                check("generic name: no prefix match to another campus",
                      not r.ok and committed("school") == "", f"{committed('school')!r}")

                # 5. Exact name with Ashby's "Name / Country / domain" result lines.
                load("alias")
                r = put("School", want)
                check("exact: multi-line catalogue result matched on its name line",
                      r.ok and not r.substituted and committed("school") == want,
                      f"ok={r.ok} {committed('school')!r} {r.note!r}")

                # 6. The catalogue offers an escape entry and no true school.
                load("escape")
                r = put("School", "LIIT Springfield")
                check("escape: the catalogue's own 'Other' entry is chosen",
                      r.ok and r.substituted == "Other" and committed("school") == "Other",
                      f"ok={r.ok} sub={r.substituted!r} {committed('school')!r}")
                check("escape: no radio of another field was clicked", radios() == [], str(radios()))

                # 7. A click that does not commit is a failure, not "verified".
                load("nocommit")
                r = put("School", want)
                check("nocommit: uncommitted typed text is not a readback success",
                      not r.ok and committed("school") == "", f"ok={r.ok} readback={r.readback!r}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
