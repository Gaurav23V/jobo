#!/usr/bin/env python3
"""Oracle Recruiting CX dropdowns, made-up postal codes, and the field-set cap (GHO-101).

Larkspur 3015 (Oracle Recruiting CX, 2026-10-30) ran out of steps one page
before Submit:
  * Every Oracle dropdown (Pin Code, State, Degree) came back "the dropdown
    produced no options". Oracle's cx-select renders its options as
    role=gridcell rows in a role=grid list portalled to <body>, linked only by
    the input's aria-controls; the setter only looked for role=option.
  * The agent asked to set the required Pin Code to a city's generic PIN, then
    part of it, then the city name. The candidate facts hold no PIN code.
  * Sixteen yes/no answers, each set first time, one per turn, used up the
    25-request cap, and the rest of the page was done by hand.

[1] form_filler against scripts/oracle_cx_select_probe.html (a copy of the
    cx-select shape from Oracle's own bundle), in a self-launched headless
    Chromium on a random port; never the production browser or a real site.
    Every check reads the page's own committed state.
[2] _resolve_field refuses a postal code / street number the facts lack.
[3] the Hermes plugin refuses the same value typed with browser_type.
[4] the driver loop: new fields set first time are not charged to the cap;
    repeats and failures still are.
"""
import json
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

PROBE = Path(__file__).with_name("oracle_cx_select_probe.html")
RESULTS: list[bool] = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)


def browser_checks():
    print("\n[1] Oracle cx-select dropdowns take a value")
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="jobo-oracle-cx-", ignore_cleanup_errors=True) as profile:
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
                shown = lambda id_: page.evaluate("(i) => document.getElementById(i).value", id_)
                open_lists = lambda: page.evaluate("() => document.querySelectorAll('.cx-select__options').length")

                ls = f.list_fields(cdp)
                f.remember_listing(cdp, ls)
                idx = {x.label.split(" *")[0].strip(): x.index for x in ls.fields}
                check("the listing names Pin Code, State and Degree as comboboxes",
                      all(k in idx for k in ("Pin Code", "State", "Degree"))
                      and all("combobox" in x.as_line() for x in ls.fields if x.index in
                              (idx.get("Pin Code"), idx.get("State"), idx.get("Degree"))),
                      str([x.as_line() for x in ls.fields]))

                def put(label, value):
                    return f.set_field(cdp, "#" + str(idx[label]), value, index=idx[label])

                r = put("Degree", "Bachelor of Technology - BTech")
                check("Degree: an exact option is picked from the gridcell list",
                      r.ok and committed().get("degree") == "Bachelor of Technology - BTech",
                      f"{r.note} {committed()}")
                check("Degree: the input shows the committed value and the list is closed",
                      shown("degree") == "Bachelor of Technology - BTech" and open_lists() == 0,
                      f"{shown('degree')!r} lists={open_lists()}")

                r = put("Degree", "Bachelor of Science")
                check("Degree: a true shorter name picks its option (Bachelor of Science - BS)",
                      r.ok and committed().get("degree") == "Bachelor of Science - BS", f"{r.note} {committed()}")

                r = put("State", "Lakeshore")
                check("State: a remote list that loads first still gets its pick",
                      r.ok and committed().get("state") == "Lakeshore", f"{r.note} {committed()}")

                before = committed().get("state")
                r = put("State", "Lakeshore State")
                check("State: a value only an unrelated grid shows is not picked from it",
                      not r.ok and committed().get("state") == before and shown("state") == before,
                      f"ok={r.ok} {committed()} shown={shown('state')!r}")
                check("State: the failed search leaves no list open", open_lists() == 0, str(open_lists()))

                r = put("Pin Code", "10002")
                check("Pin Code: a value the site lists is picked (the setter works there too)",
                      r.ok and committed().get("pin") == "10002", f"{r.note} {committed()}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


FACTS = ("Candidate facts:\n- Location: Springfield, USA (current).\n"
         "- Experience since 2023 at Example Corp.\n")


def resolve_checks():
    print("\n[2] NEED_FIELD refuses a postal code or street number the facts do not give")
    calls = []
    saved = (f.set_field, f.listed_field, personal.candidate_facts, personal.candidate)
    labels = {19: ("combobox", "Pin Code *"), 15: ("text", "Address Line 1"), 18: ("text", "City or Town")}
    f.listed_field = lambda cdp_url, index: labels.get(index)
    f.set_field = lambda *a, **k: calls.append(a) or f.SetFieldResult(ok=True, field_name="x", value=a[2],
                                                                    matched="m", strategy="s")
    facts = {"text": FACTS}
    personal.candidate_facts = lambda: facts["text"]
    personal.candidate = lambda: {"location": "Springfield, USA"}
    try:
        def run(line, fails=None):
            fails = {} if fails is None else fails
            return agent_driver._resolve_field(line, cdp_url="http://127.0.0.1:1", job_id=3015, failures=fails), fails

        for value in ("10001", "100", "Springfield"):
            calls.clear()
            msg, fails = run(f"NEED_FIELD: #19 = {value}")
            check(f"Pin Code = {value!r} is refused before anything is typed",
                  msg.startswith("FIELD_SET_REFUSED") and not calls and fails.get("#19") == 1, msg[:160])
        check("the refusal says to leave it empty and when to stop stuck",
              "Leave this field empty" in msg and "STATUS: stuck" in msg and "Pin Code" in msg, msg)

        calls.clear()
        msg, _ = run("NEED_FIELD: #15 = 12 Main Street")
        check("Address Line 1 with a made-up house number is refused", msg.startswith("FIELD_SET_REFUSED") and not calls, msg[:120])
        calls.clear()
        msg, _ = run("NEED_FIELD: #15 = Springfield")
        check("Address Line 1 = the facts' city still goes through", msg.startswith("FIELD_SET:") and calls, msg[:120])
        calls.clear()
        msg, _ = run("NEED_FIELD: #18 = Springfield")
        check("a city field is untouched by the rule", msg.startswith("FIELD_SET:") and calls, msg[:120])
        calls.clear()
        msg, _ = run('NEED_FIELD: #19 = ""')
        check("clearing the Pin Code is not refused", not msg.startswith("FIELD_SET_REFUSED"), msg[:120])

        facts["text"] = FACTS + "- PIN code: 10001\n"
        calls.clear()
        msg, _ = run("NEED_FIELD: #19 = 10001")
        check("once the facts give a PIN code, exactly that one goes through", msg.startswith("FIELD_SET:") and calls, msg[:120])
        calls.clear()
        msg, _ = run("NEED_FIELD: #19 = 10002")
        check("a different PIN code is still refused", msg.startswith("FIELD_SET_REFUSED") and not calls, msg[:120])
        facts["text"] = FACTS

        calls.clear()
        msg, _ = run("NEED_FIELD: Zip Code = 62704")
        check("a prose hint naming a ZIP field is checked too", msg.startswith("FIELD_SET_REFUSED") and not calls, msg[:120])
    finally:
        f.set_field, f.listed_field, personal.candidate_facts, personal.candidate = saved

    for label, value, kind in (("Enter the PIN we sent you", "483920", ""), ("Postal Address", "Springfield, USA", ""),
                               ("Email Address", "a1@example.com", ""), ("Zip/Postal Code", "62704", "postal"),
                               ("Pincode", "Springfield", "postal")):
        got = made_up_values.refusal(label, value, FACTS)
        check(f"rule: {label!r} = {value!r} -> {kind or 'allowed'}", got == kind, repr(got))


def plugin_checks():
    print("\n[3] browser_type is held to the same rule (Hermes plugin)")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "jobo_browser_fact_guard_test", ROOT / "hermes_plugin" / "jobo-browser" / "fact_guard.py")
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    fg.facts_text = lambda: FACTS
    names = {"e50": "combobox Pin Code", "e44": "textbox Address Line 1", "e47": "textbox City or Town"}
    ref_name = lambda ref: names.get(str(ref or "").lstrip("@"), "")
    blocked = fg.check_tool_call("browser_type", {"ref": "e50", "text": "10001"}, ref_name)
    check("typing a made-up PIN into the Pin Code box is blocked",
          bool(blocked) and blocked.startswith("BLOCKED by jobo") and "Pin Code" in blocked, str(blocked))
    check("a made-up house number in Address Line 1 is blocked",
          bool(fg.check_tool_call("browser_type", {"ref": "@e44", "text": "12 Main Street"}, ref_name)))
    check("the facts' city in City or Town goes through",
          fg.check_tool_call("browser_type", {"ref": "e47", "text": "Springfield"}, ref_name) is None)
    check("an unknown ref goes through (fails open)",
          fg.check_tool_call("browser_type", {"ref": "e99", "text": "10001"}, ref_name) is None)
    check("other tools are not touched", fg.check_tool_call("browser_click", {"ref": "e50"}, ref_name) is None)
    text = (ROOT / "hermes_plugin" / "jobo-browser" / "__init__.py").read_text()
    check("the plugin runs the fact guard in pre_tool_call", "fact_guard.check_tool_call(" in text)


VERDICT = "STATUS: submitted\nDETAIL: done\nFIELDS_FILLED_JSON: {}\n"


def _drive(rounds, resolve):
    calls, sets = [], []

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append(prompt)
        i = len(calls) - 1
        return (rounds[i] if i < len(rounds) else "session_id: s1\n" + VERDICT), False

    def resolve_field(line, *, cdp_url, job_id, failures):
        sets.append(line.strip())
        return resolve(line.strip())

    names = ("_run_hermes", "_resolve_field", "_resolve_fields_listing", "_dump_debug_transcript", "session_api_calls")
    saved = [getattr(agent_driver, k) for k in names]
    fakes = (run_hermes, resolve_field, lambda **k: "FIELDS_LISTED: 0", lambda *a, **k: None, lambda *a, **k: 1)
    for k, v in zip(names, fakes):
        setattr(agent_driver, k, v)
    try:
        agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url="http://127.0.0.1:1",
                                     job_id=3015, resume_path="/tmp/r.pdf")
    finally:
        for k, v in zip(names, saved):
            setattr(agent_driver, k, v)
    return calls, sets


def driver_checks():
    print("\n[4] a long run of first-time sets does not burn the field-set cap")
    cap = agent_driver.MAX_FIELD_ROUNDS
    radio = lambda line: f"FIELD_SET: Q{line.split('#')[1].split(' ')[0]} | Yes = 'true' (via trusted-radio)"
    n = cap + 15
    one_per_turn = [f"session_id: s1\nNEED_FIELD: #{i} = Yes\n" for i in range(1, n + 1)]
    calls, sets = _drive(one_per_turn, radio)
    check(f"{n} different fields, one per turn, all run", len(sets) == n, str(len(sets)))
    check("no request-limit reply on the way", not any("request limit" in c for c in calls))

    batch = ["session_id: s1\n" + "".join(f"NEED_FIELD: #{i} = No\n" for i in range(1, n + 1))]
    calls, sets = _drive(batch, radio)
    check(f"{n} different fields in one batch all run in one resume",
          len(sets) == n and len(calls) == 2 and "request limit" not in calls[1], f"{len(sets)} {len(calls)}")

    same = ["session_id: s1\nNEED_FIELD: #7 = Yes\n"] * (cap + 10)
    calls, sets = _drive(same, lambda line: "FIELD_SET: Q7 | Yes = 'true' (via trusted-radio)")
    check(f"re-setting one field is still capped at {cap} charged requests", len(sets) == cap + 1, str(len(sets)))

    fails = [f"session_id: s1\nNEED_FIELD: #{i} = x\n" for i in range(1, cap + 10)]
    calls, sets = _drive(fails, lambda line: "FIELD_SET_FAILED: the dropdown produced no options")
    check(f"failed sets are still capped at {cap}", len(sets) == cap, str(len(sets)))

    refunds = agent_driver.MAX_NEW_FIELD_REFUNDS
    many = ["session_id: s1\n" + "".join(f"NEED_FIELD: #{i} = No\n" for i in range(1, refunds + cap + 20))]
    calls, sets = _drive(many, radio)
    check("the refund is bounded: a runaway still stops",
          len(sets) == refunds + cap and "request limit" in calls[1], f"{len(sets)}")

    footer = f.ListFieldsResult(ok=True, fields=[f.FieldInfo(index=1, label="Yes", kind="radio")]).as_agent_message()
    check("the field listing asks for every answer in ONE reply", "ONE reply" in footer, footer[-200:])


def main() -> int:
    browser_checks()
    resolve_checks()
    plugin_checks()
    driver_checks()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
