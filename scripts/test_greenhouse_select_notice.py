#!/usr/bin/env python3
"""Greenhouse react-select "No options" notice and the current-title rule (GHO-117).

Razorpay 3468 (job-boards.greenhouse.io, 2026-10-31) stuck on its required
"Current Designation" dropdown. Every set came back 0.3s later with
"no option matched ... The site offers: No options":
  * react-select renders its empty state as
    `select__menu-notice--no-options`, and the setter's `[class*="-option"]`
    took that notice for the one option on offer. It stopped waiting at once
    and never showed the agent the real list.
  * the list it hid is three career stages (Experienced Professional, ...),
    the same as Current Career Stage. Any typed title filters it to nothing,
    so the agent needed the unfiltered list.
  * the agent asked for "Senior" and "Senior Software Engineer"; the candidate
    facts give a current title with no seniority.

[1] form_filler against scripts/greenhouse_select_notice_probe.html, in a
    self-launched headless Chromium on a random port; never the production
    browser or a real site. Checks read the page's own committed state.
[2] _resolve_field refuses a current title with a seniority the facts lack.
[3] the Hermes plugin refuses the same value typed with browser_type.
"""
import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver  # noqa: E402
from module4 import form_filler as f  # noqa: E402
from policy import made_up_values, personal  # noqa: E402

PROBE = Path(__file__).with_name("greenhouse_select_notice_probe.html")
STAGES = ["Experienced Professional", "College Grads / Fresher", "Student / College Intern"]
RESULTS: list[bool] = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)


def browser_checks():
    print("\n[1] react-select notices are not options; a failed search lists the whole menu")
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="jobo-gh-notice-", ignore_cleanup_errors=True) as profile:
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
                page.goto(PROBE.as_uri())
                page.wait_for_timeout(200)
                committed = lambda: page.evaluate("() => ({...window.committed})")
                typed = lambda id_: page.evaluate("(i) => document.getElementById(i).value", id_)
                menus = lambda: page.evaluate("() => document.querySelectorAll('.select__menu').length")

                ls = f.list_fields(cdp)
                f.remember_listing(cdp, ls)
                idx = {x.label.split("*")[0].strip(): x.index for x in ls.fields}
                check("the listing names Current Designation and Current Location",
                      "Current Designation" in idx and "Current Location" in idx,
                      str([x.as_line() for x in ls.fields]))

                def put(label, value):
                    return f.set_field(cdp, "#" + str(idx[label]), value, index=idx[label])

                r = put("Current Designation", "Senior Software Engineer")
                check("Designation: a title the list lacks is not set",
                      not r.ok and not committed().get("designation"), f"ok={r.ok} {committed()}")
                check("Designation: the reply lists the three values the site offers, not 'No options'",
                      r.available_options == STAGES and "No options" not in (r.note or ""),
                      f"{r.available_options} note={r.note!r}")
                check("Designation: the failed search leaves no query and no open menu",
                      typed("question_1") == "" and menus() == 0, f"{typed('question_1')!r} menus={menus()}")

                r = put("Current Designation", "Experienced Professional")
                check("Designation: a listed value is picked",
                      r.ok and committed().get("designation") == "Experienced Professional",
                      f"{r.note} {committed()}")

                r = put("Current Location", "Springfield")
                check("Location: a remote search that shows 'No options' then 'Loading...' still gets its pick",
                      r.ok and committed().get("location") == "Springfield, USA", f"{r.note} {committed()}")

                before = committed().get("location")
                r = put("Current Location", "Atlantis")
                check("Location: a search with no results is reported as empty, not as an option named 'No options'",
                      not r.ok and r.empty_remote and not r.available_options
                      and "No options" not in (r.note or "") and committed().get("location") == before,
                      f"ok={r.ok} empty={r.empty_remote} opts={r.available_options} note={r.note!r}")
                check("Location: the failed search leaves no query and no open menu",
                      typed("question_2") == "" and menus() == 0, f"{typed('question_2')!r} menus={menus()}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


FACTS = ("Candidate facts:\n- Current employer: Example Corp, Backend Engineer, Springfield.\n"
         "- Location: Springfield, USA (current).\n")


def resolve_checks():
    print("\n[2] NEED_FIELD refuses a current title with a seniority the facts do not give")
    calls = []
    saved = (f.set_field, f.listed_field, personal.candidate_facts, personal.candidate)
    labels = {23: ("combobox", "Current Designation* | question_1"), 17: ("combobox", "Current Career Stage*"),
              5: ("text", "Current Job Title")}
    f.listed_field = lambda cdp_url, index: labels.get(index)
    f.set_field = lambda *a, **k: calls.append(a) or f.SetFieldResult(ok=True, field_name="x", value=a[2],
                                                                    matched="m", strategy="s")
    personal.candidate_facts = lambda: FACTS
    personal.candidate = lambda: {}
    try:
        def run(line):
            fails = {}
            return agent_driver._resolve_field(line, cdp_url="http://127.0.0.1:1", job_id=3468, failures=fails), fails

        for value in ("Senior Software Engineer", "Senior", "Lead Backend Engineer"):
            calls.clear()
            msg, fails = run(f"NEED_FIELD: #23 = {value}")
            check(f"Current Designation = {value!r} is refused before anything is set",
                  msg.startswith("FIELD_SET_REFUSED") and not calls and fails.get("#23") == 1, msg[:160])
        check("the refusal points the agent at the facts' current title",
              "CURRENT title" in msg and "candidate facts" in msg, msg)
        calls.clear()
        msg, _ = run("NEED_FIELD: #5 = Senior Backend Engineer")
        check("a Current Job Title text box is held to the same rule",
              msg.startswith("FIELD_SET_REFUSED") and not calls, msg[:120])
        for n, value in ((23, "Backend Engineer"), (23, "Experienced Professional"), (17, "Experienced Professional")):
            calls.clear()
            msg, _ = run(f"NEED_FIELD: #{n} = {value}")
            check(f"#{n} = {value!r} goes through", msg.startswith("FIELD_SET:") and calls, msg[:120])
    finally:
        f.set_field, f.listed_field, personal.candidate_facts, personal.candidate = saved

    for label, value, kind in (("Current Designation", "Senior SDE", "title"),
                               ("Present Role", "Engineering Manager", "title"),
                               ("Current Designation", "Backend Engineer", ""),
                               ("Job title of the role you are applying for", "Senior SDE", ""),
                               ("Current Career Stage", "Senior", "")):
        got = made_up_values.refusal(label, value, FACTS)
        check(f"rule: {label!r} = {value!r} -> {kind or 'allowed'}", got == kind, repr(got))
    check("a seniority the facts DO give is allowed",
          made_up_values.refusal("Current Designation", "Senior Backend Engineer",
                                 FACTS.replace("Backend Engineer", "Senior Backend Engineer")) == "")


def plugin_checks():
    print("\n[3] browser_type is held to the same rule (Hermes plugin)")
    spec = importlib.util.spec_from_file_location(
        "jobo_browser_fact_guard_gho117", ROOT / "hermes_plugin" / "jobo-browser" / "fact_guard.py")
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    fg.facts_text = lambda: FACTS
    names = {"e23": "combobox Current Designation*", "e24": "combobox Current Location*"}
    ref_name = lambda ref: names.get(str(ref or "").lstrip("@"), "")
    blocked = fg.check_tool_call("browser_type", {"ref": "e23", "text": "Senior Software Engineer"}, ref_name)
    check("typing a made-up seniority into Current Designation is blocked",
          bool(blocked) and blocked.startswith("BLOCKED by jobo") and "Current Designation" in blocked, str(blocked))
    check("the facts' title goes through",
          fg.check_tool_call("browser_type", {"ref": "e23", "text": "Backend Engineer"}, ref_name) is None)
    check("another field is untouched",
          fg.check_tool_call("browser_type", {"ref": "e24", "text": "Senior"}, ref_name) is None)


def main():
    browser_checks()
    resolve_checks()
    plugin_checks()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
