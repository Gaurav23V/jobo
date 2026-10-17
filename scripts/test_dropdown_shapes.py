#!/usr/bin/env python3
"""Dropdown shapes from real forms that stalled the apply agent (2026-10-01).

Runs NEED_FIELDS / NEED_FIELD (form_filler.list_fields / set_field) against
scripts/dropdown_shapes_probe.html, a local copy of:
  * Workday button-listbox dropdowns (Plover/Heron "State", "Phone Device Type"):
    the old code listed the hidden id-store input instead of the button, so
    every pick was discarded ("the value did not survive").
  * Workday search prompts (Skills / How Did You Hear): type, search, click a
    result; never click the echo row that just repeats the typed text.
  * Angular Material mat-select (Bellows/Juniperx): picks worked on the page but
    were reported as failures because the accessible name gains the value.
  * A consent checkbox with only a sentence beside it.

Self-isolated: launches its own headless Chromium on a random port, never the
production browser, never a real site. Every check reads the page itself, not
the tool's own report.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import form_filler as f  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("dropdown_shapes_probe.html")


def main() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)

    with tempfile.TemporaryDirectory(prefix="jobo-dropdowns-", ignore_cleanup_errors=True) as profile:
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

                def load():
                    page.goto(PROBE.as_uri())
                    page.wait_for_timeout(200)

                def listing():
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    return ls

                def find(ls, label):
                    return next((x for x in ls.fields if x.label.lower().startswith(label.lower())), None)

                def put(ls, label, value):
                    fi = find(ls, label)
                    if not fi:
                        return f.SetFieldResult(False, note="not listed"), None
                    return f.set_field(cdp, "#" + str(fi.index), value, index=fi.index), fi

                btn_text = lambda id_: page.evaluate("(i)=>document.getElementById(i).innerText.trim()", id_)
                pills = lambda id_: page.evaluate(
                    "(i)=>[...document.getElementById(i).closest('[data-automation-id=multiselectInputContainer]')"
                    ".querySelectorAll('[data-automation-id=selectedItem]')].map(n=>n.title)", id_)
                mat_text = lambda id_: page.evaluate(
                    "(i)=>document.getElementById(i).querySelector('.mat-mdc-select-value-text').innerText.trim()", id_)
                lists_open = lambda: page.evaluate(
                    "()=>document.querySelectorAll('.wd-list, .results, .overlay').length")

                # ---- Workday button-listbox ----
                load()
                ls = listing()
                dev = find(ls, "Phone Device Type")
                check("Workday dropdown listed as the button, as a dropdown",
                      dev is not None and dev.kind == "combobox", str(dev))
                check("hidden id-store inputs are not listed",
                      not any(x.label in ("Select One", "United Kingdom") or x.value.startswith("id-")
                              for x in ls.fields), str([x.label for x in ls.fields]))
                check("current value shown for a Workday dropdown",
                      (find(ls, "Country Phone Code") or f.FieldInfo(0, "", "", "", "")).value == "United Kingdom (+44)")
                r, _ = put(ls, "Phone Device Type", "Mobile")
                check("Workday pick sticks on the page", r.ok and btn_text("input-5") == "Mobile", r.note)
                r, _ = put(ls, "Phone Device Type", "Landline")
                check("same field re-set from the SAME listing (label change is not a new field)",
                      r.ok and btn_text("input-5") == "Landline", r.note)
                r, _ = put(ls, "Country", "Zimbabwe")
                check("last option of a 236-item self-scrolling list", r.ok and btn_text("input-1") == "Zimbabwe", r.note)
                r, _ = put(listing(), "Country", "Åland Islands")
                check("second option of the same list", r.ok and btn_text("input-1") == "Åland Islands", r.note)
                r, _ = put(listing(), "Country Phone Code", "India (+91)")
                check("dropdown far down the page under a sticky footer",
                      r.ok and btn_text("input-6") == "India (+91)", r.note)
                r, _ = put(listing(), "Phone Device Type", "Satellite")
                check("value the dropdown does not offer is refused with its options",
                      not r.ok and r.no_match and "Mobile" in r.available_options
                      and btn_text("input-5") == "Landline", r.note)
                check("no Workday list left open", lists_open() == 0)

                # ---- Workday search prompt with a catalogue ----
                r, _ = put(listing(), "Type to Add Skills", "Python (Programming Language)")
                check("search prompt: real result picked", r.ok and pills("skills") == ["Python (Programming Language)"], r.note)
                check("search prompt: results closed after a pick", lists_open() == 0)
                r, _ = put(listing(), "Type to Add Skills", "Zzzqx Nonsense")
                check("search prompt: echo row never clicked, value refused",
                      not r.ok and r.no_match and pills("skills") == ["Python (Programming Language)"],
                      f"{r.note} pills={pills('skills')}")
                check("search prompt: refusal closes the results too", lists_open() == 0)
                r, _ = put(listing(), "Type to Add Skills", "SQLite")
                check("search prompt: second pick after a refusal",
                      r.ok and pills("skills") == ["Python (Programming Language)", "SQLite"], r.note)
                r, _ = put(listing(), "Type to Add Skills", "SQLite")
                check("search prompt: re-asking an existing pick does not toggle it off",
                      r.ok and pills("skills") == ["Python (Programming Language)", "SQLite"], r.note)
                sk = find(listing(), "Type to Add Skills")
                check("search prompt listing shows the picked values",
                      sk is not None and "SQLite" in sk.value and sk.kind == "combobox", str(sk))

                # ---- Workday search prompt without a catalogue (free text) ----
                r, _ = put(listing(), "Hobbies", "Chess")
                check("free-text prompt: saved once, said plainly",
                      r.ok and r.strategy == "workday-prompt-freetext" and pills("hobbies") == ["Chess"],
                      f"{r.strategy} {r.note} pills={pills('hobbies')}")
                r, _ = put(listing(), "Hobbies", "Running")
                check("free-text prompt: no leftover text turned into extra pills",
                      r.ok and pills("hobbies") == ["Chess", "Running"], f"pills={pills('hobbies')}")
                check("free-text prompt: results closed", lists_open() == 0)

                # ---- Angular Material mat-select ----
                load()
                first = listing()
                sigs = [x.sig for x in first.fields]
                r, _ = put(first, "Gender", "Female")
                check("mat-select pick reported as the success it is", r.ok and mat_text("mat-select-2") == "Female", r.note)
                r, _ = put(first, "UG Qualification", "B.Tech/B.E.")
                check("second mat-select from the SAME listing", r.ok and mat_text("mat-select-4") == "B.Tech/B.E.", r.note)
                r, _ = put(first, "Gender", "Male")
                check("re-set a picked mat-select from the old listing", r.ok and mat_text("mat-select-2") == "Male", r.note)
                after = listing()
                check("field numbers and identities unchanged after picks",
                      [x.sig for x in after.fields] == sigs and find(after, "Gender").label == "Gender",
                      str([(a.label, b) for a, b in zip(after.fields, sigs) if a.sig != b]))
                r, _ = put(after, "Are you authorized", "No")
                check("mat-select under its own floating question label", r.ok and mat_text("mat-select-18") == "No", r.note)
                check("no mat-select panel left open", lists_open() == 0)

                # ---- consent checkbox with only a sentence beside it ----
                cb = next((x for x in listing().fields if x.kind == "checkbox"), None)
                check("unlabelled consent checkbox listed by its sentence",
                      cb is not None and cb.label.startswith("BELLOWS Limited"), str(cb))
                r = f.set_field(cdp, "#" + str(cb.index), "true", index=cb.index)
                check("consent checkbox ticked by number",
                      r.ok and page.evaluate("()=>document.querySelector('[data-testid=terms-checkbox]').checked"), r.note)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    print(f"{sum(results)}/{len(results)} passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
