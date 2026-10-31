#!/usr/bin/env python3
"""Ashby's location typeahead and essay boxes that mention a password (GHO-115).

Blueyonder 2502 (Ashby, 2026-10-31) ended stuck with nothing sent:
  * Current Location never held. The setter typed the city, then read the
    page's always-visible checkbox and yes/no groups (their classes contain
    "-option") as the results before the places search answered, found no
    city there and "substituted" the gender question's "Not listed" checkbox.
    The location box only held the typed text, which Ashby wipes on blur, so
    Submit kept saying the required field was missing.
  * The essay question "Why <Brand>Password?" was taken for an account
    password field because its label contains "Password", and the pipeline's
    standard password was typed in as the answer.

[1] form_filler against scripts/ashby_location_probe.html (the Ashby shape),
    in a self-launched headless Chromium on a random port; never the
    production browser or a real site. Checks read the page's own state.
[2] _resolve_field leaves an essay box's answer alone and still puts the
    standard password into real password fields.
[3] the Hermes plugin's password rewrite makes the same call from a snapshot.
"""
import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver, password_reset  # noqa: E402
from module4 import form_filler as f  # noqa: E402

PROBE = Path(__file__).with_name("ashby_location_probe.html")
RESULTS: list[bool] = []
STANDARD = "Example#Pass2026"


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)


def browser_checks():
    print("\n[1] Ashby Current Location takes a real place, and nothing else is clicked")
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="jobo-ashby-loc-", ignore_cleanup_errors=True) as profile:
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

                def fresh():
                    page.goto(PROBE.as_uri())
                    page.wait_for_timeout(200)
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    return {x.label.split(" *")[0].strip(): x.index for x in ls.fields}, ls

                committed = lambda: page.evaluate("() => window.committed.location")
                ticked = lambda: page.evaluate(
                    "() => [...document.querySelectorAll('input[type=checkbox]:checked')].map(c => c.name)")
                pressed = lambda: page.evaluate(
                    "() => [...document.querySelectorAll('[aria-pressed=true]')].map(b => b.textContent)")

                def after_blur():
                    page.evaluate("() => { document.getElementById('loc').blur(); "
                                  "document.getElementById('_systemfield_name').focus(); }")
                    page.wait_for_timeout(100)
                    return page.evaluate("() => document.getElementById('loc').value")

                idx, ls = fresh()
                check("the listing shows Current Location as a combobox and the essay box as text",
                      "Current Location" in idx and "Why 9Password?" in idx
                      and ls.fields[idx["Current Location"] - 1].kind == "combobox"
                      and ls.fields[idx["Why 9Password?"] - 1].kind == "text",
                      str([x.as_line() for x in ls.fields]))
                loc = idx["Current Location"]

                r = f.set_field(cdp, f"#{loc}", "Springfield, India", index=loc)
                check("a city the search spells differently commits the search's own result",
                      r.ok and committed() == "Springfield, Illinois, India"
                      and r.substituted == "Springfield, Illinois, India",
                      f"ok={r.ok} sub={r.substituted!r} committed={committed()!r} note={r.note[:160]}")
                check("no checkbox or yes/no button on the page was clicked",
                      ticked() == [] and pressed() == [], f"{ticked()} {pressed()}")
                check("the pick survives leaving the field (it is a real selection)",
                      after_blur() == "Springfield, Illinois, India", repr(after_blur()))

                idx, _ = fresh()
                r = f.set_field(cdp, f"#{loc}", "India", index=loc)
                check("an exact place is picked first time",
                      r.ok and committed() == "India" and not r.substituted, f"{r.note[:160]} {committed()!r}")
                check("...without touching the gender checkboxes", ticked() == [], str(ticked()))

                idx, _ = fresh()
                r = f.set_field(cdp, f"#{loc}", "Atlantis", index=loc)
                check("a place the search does not know fails honestly",
                      not r.ok and committed() == "" and not r.substituted, f"ok={r.ok} {r.readback!r}")
                check("...and the failure clicks no other control",
                      ticked() == [] and pressed() == [], f"{ticked()} {pressed()}")
                check("...and leaves no typed text behind", after_blur() == "", repr(after_blur()))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def driver_checks():
    print("\n[2] NEED_FIELD: an essay box keeps its answer, password fields get the standard one")
    calls = []
    labels = {
        25: ("text", "Why 9Password?"),
        26: ("textarea", "Tell us how you use a password manager"),
        27: ("text", "Close relationships at 9Password"),
        3: ("password", "Password *"),
        4: ("password", "Confirm"),
        5: ("text", "Confirm Password"),
        6: ("text", "Do you use a password manager?"),
    }
    saved = (f.set_field, f.listed_field, password_reset.STANDARD_PASSWORD,
             agent_driver._recorded_site_password, agent_driver._made_up_value_refusal)
    f.listed_field = lambda cdp_url, index: labels.get(index)
    f.set_field = lambda *a, **k: calls.append(a[2]) or f.SetFieldResult(
        ok=True, field_name="x", value=a[2], matched="m", strategy="s")
    password_reset.STANDARD_PASSWORD = STANDARD
    agent_driver._recorded_site_password = lambda *a, **k: ("", "")
    agent_driver._made_up_value_refusal = lambda *a, **k: ""
    try:
        def run(line):
            calls.clear()
            msg = agent_driver._resolve_field(line, cdp_url="http://127.0.0.1:1", job_id=2502, failures={})
            return msg, (calls[0] if calls else None)

        essay = "I have used the product daily for years."
        for n in (25, 26, 27, 6):
            msg, typed = run(f"NEED_FIELD: #{n} = {essay}")
            check(f"#{n} {labels[n][1]!r}: the answer is typed as given",
                  typed == essay and STANDARD not in msg, f"typed={typed!r}")
        msg, typed = run(f"NEED_FIELD: Why 9Password? = {essay}")
        check("a prose hint naming the essay box keeps its answer", typed == essay, repr(typed))

        for n in (3, 4, 5):
            msg, typed = run(f"NEED_FIELD: #{n} = Alex@Invented2026!")
            check(f"#{n} {labels[n][1]!r}: an invented password becomes the standard one",
                  typed == STANDARD and "never invented" in msg, f"typed={typed!r}")
        msg, typed = run("NEED_FIELD: Password = Alex@Invented2026!")
        check("a prose hint 'Password' still gets the standard one", typed == STANDARD, repr(typed))
    finally:
        (f.set_field, f.listed_field, password_reset.STANDARD_PASSWORD,
         agent_driver._recorded_site_password, agent_driver._made_up_value_refusal) = saved


def plugin_checks():
    print("\n[3] browser_type into an essay box is not rewritten to the password (Hermes plugin)")
    spec = importlib.util.spec_from_file_location(
        "jobo_browser_account_guard_ashby_test", ROOT / "hermes_plugin" / "jobo-browser" / "account_guard.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    g.STANDARD_PASSWORD = STANDARD
    g.SITE_PASSWORDS = {}
    g.reset_state()
    g.remember_refs('- textbox "Why 9Password?" [ref=e25]\n'
                    '- textbox "Close relationships at 9Password" [ref=e27]\n'
                    '- combobox "Password manager you use" [ref=e28]\n'
                    '- textbox "Password *" [ref=e3]\n'
                    '- textbox "Confirm Password" [ref=e4]\n'
                    '- button "Show password" [ref=e5]')
    essay = "I have used the product daily for years."
    for ref in ("e25", "e27", "e28"):
        check(f"{ref}: not a password field, typed text kept",
              not g.is_password_ref("@" + ref)
              and g.password_rewrite("browser_type", {"ref": "@" + ref, "text": essay}) is None
              and g.check_tool_call("browser_type", {"ref": "@" + ref, "text": essay}) is None)
    for ref in ("e3", "e4"):
        check(f"{ref}: a password field still gets the standard password",
              g.password_rewrite("browser_type", {"ref": "@" + ref, "text": "Alex@Invented2026!"})
              == {"text": STANDARD})
    check("the show-password toggle is not a password field", not g.is_password_ref("@e5"))


def main() -> int:
    browser_checks()
    driver_checks()
    plugin_checks()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
