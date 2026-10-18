#!/usr/bin/env python3
"""Workday's two-level "How Did You Hear About Us?" prompt (GHO-55).

Two Workday jobs (runs of 2026-10-15 and 2026-10-18 22:00) got stuck on
step 1: every NEED_FIELD came back "no matching option", "exact widget is
obscured" or "did not persist", and the agent's own typing left text that
Save and Continue cleared. Measured on the live form: the search box's own
click opens the CATEGORY list ~600 ms later, after the setter had already
typed, so the setter clicked the "Job Board" category row (which only opens
its sub-list) instead of the "Job Board" option. Rows also ignore the pointer
after the list scrolls (1-1.6 s in the headed apply window), which read as
"obscured". Search never matches a category name ("Social Media"), and the
virtualised category list had been offered to the agent as the field's only
values.

Runs NEED_FIELDS / NEED_FIELD against scripts/workday_source_prompt_probe.html
in its own headless Chromium on a random port, never the production browser.
Every check reads the page itself, not the tool's own report.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import form_filler as f  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("workday_source_prompt_probe.html")
LABEL = "How Did You Hear"


def main() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)

    with tempfile.TemporaryDirectory(prefix="jobo-wd-source-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + profile, "about:blank"],
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
                page.set_viewport_size({"width": 1280, "height": 900})

                def put(value, variant=""):
                    page.goto("about:blank")  # a bare #hash change would not reload
                    page.goto(PROBE.as_uri() + variant)
                    page.wait_for_timeout(200)
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    fi = next((x for x in ls.fields if x.label.startswith(LABEL)), None)
                    if not fi:
                        return f.SetFieldResult(False, note="not listed")
                    return f.set_field(cdp, "#" + str(fi.index), value, index=fi.index)

                pills = lambda: page.evaluate(
                    "()=>[...document.querySelectorAll('[data-automation-id=selectedItem] p')].map(n=>n.textContent)")
                picks = lambda: page.evaluate("()=>window.__picks")
                popup_open = lambda: page.evaluate(
                    "()=>document.querySelectorAll('[data-automation-id=responsiveMonikerPrompt]').length")

                r = put("Job Board")
                check("option whose name is also a category: the option is picked",
                      r.ok and pills() == ["Job Board"] and picks() == ["Job Board"],
                      f"{r.note} pills={pills()} picks={picks()}")
                check("list closed after the pick", popup_open() == 0)

                r = put("LinkedIn")
                check("option found by search among near matches",
                      r.ok and pills() == ["LinkedIn"], f"{r.note} pills={pills()}")

                r = put("Social Media")
                check("a category is never picked; its own options are offered",
                      not r.ok and r.no_match and pills() == [] and
                      r.available_options == ["Facebook", "Glassdoor", "LinkedIn", "YouTube"],
                      f"{r.note} options={r.available_options} pills={pills()}")
                check("list closed after refusing a category", popup_open() == 0)

                r = put("Social Media > YouTube")
                check("category > option path picks that option",
                      r.ok and pills() == ["YouTube"] and picks() == ["YouTube"],
                      f"{r.note} pills={pills()} picks={picks()}")

                r = put("Television", "#no-other")
                offered = r.available_options or []
                check("no match offers the whole category list, past the rendered rows",
                      not r.ok and r.no_match and pills() == [] and len(offered) == 21
                      and "Social Media" in offered and "University/College" in offered,
                      f"{r.note} options={offered}")
                check("list closed after no match", popup_open() == 0)

                # "Other" is a group far down the list; the generic escape
                # hatch (answer_book) can only use it once the list is walked.
                r = put("Television")
                check("unlisted source falls back to the site's own Other option",
                      r.ok and r.substituted == "Other" and pills() == ["Other"],
                      f"{r.note} sub={r.substituted!r} pills={pills()}")
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    print(f"{sum(results)}/{len(results)} passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
