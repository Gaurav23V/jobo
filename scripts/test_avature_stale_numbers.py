#!/usr/bin/env python3
"""Avature rows inserted mid-batch must not burn the field-set cap (GHO-23).

Live failure (14:29 round, 2026-10-15, Kestrel 1622 and 1628): setting
Country/State, or a college of "Other", makes Avature insert new rows, so
every later number in the agent's NEED_FIELD batch pointed at a different
control. Each was refused "the form changed since the fields were listed",
each was charged to the 25-set cap, and 1628 hit the cap with 17 of them.

Covers:
  * in the page: a listed control that only MOVED is found again by its
    signature and set (moved_to reported); one that is really gone or is no
    longer unique is still refused, with nothing typed.
  * in the driver: a stale reply holds back the rest of the batch, is not
    charged to MAX_FIELD_ROUNDS or the field's strikes, and the form is
    re-listed for the agent in the same resume.

Self-isolated: own headless Chromium on a random port, never 9333.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import agent_driver, form_filler as f  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# Kestrel's Avature shape: picking a country inserts the State row under it,
# picking "Other" college inserts a free-text name row.
FORM = """<!doctype html><html><body><form id=f>
<div class=row><label for="10940">Country *</label>
  <select id="10940" name="10940"><option value=""></option><option>India</option><option>USA</option></select></div>
<div class=row id=college-row><label for="10955">What is the name of your college? *</label>
  <select id="10955" name="10955"><option value=""></option><option>MNIT Riverton</option><option>Other</option></select></div>
<div class=row><label for="10959">Education Level</label>
  <select id="10959" name="10959"><option value=""></option><option>Bachelor's Degree</option><option>Master's Degree</option></select></div>
<div class=row><label for="10979-1-0">Job Title *</label><input id="10979-1-0" name="10979-1-0"></div>
<fieldset class=row><legend>Do you hold an Indian Passport? *</legend>
  <label><input type=radio name="10985" id="10985-y" value="1"> Yes</label>
  <label><input type=radio name="10985" id="10985-n" value="0"> No</label></fieldset>
<div class=row id=dupes><label>Notes<input name=n></label></div>
</form>
<script>
const after = (el, html) => el.closest('.row').insertAdjacentHTML('afterend', html);
document.getElementById('10940').addEventListener('change', e => {
  if (!document.getElementById('10941'))
    after(e.target, '<div class=row><label for="10941">State *</label>' +
      '<select id="10941" name="10941"><option value=""></option><option>Illinois</option></select></div>');
});
document.getElementById('10955').addEventListener('change', e => {
  if (e.target.value === 'Other' && !document.getElementById('10956'))
    after(e.target, '<div class=row><label for="10956">College name (if Other) *</label>' +
      '<input id="10956" name="10956"></div>');
});
</script></body></html>"""


def main() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)

    with tempfile.TemporaryDirectory(prefix="jobo-gho23-", ignore_cleanup_errors=True) as profile:
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
                val = lambda id_: page.evaluate("(i)=>document.getElementById(i).value", id_)

                # A file:// page, not set_content(): about:blank is not a secure
                # context, so the radio path's crypto.randomUUID() is missing.
                html = Path(profile) / "form.html"
                html.write_text(FORM)

                def load():
                    page.goto(html.as_uri())
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    return {x.label.split(" *")[0].split("?")[0]: x.index for x in ls.fields}

                n = load()
                put = lambda i, v: f.set_field(cdp, f"#{i}", v, index=i)

                # 1. The Kestrel batch: Country first, then numbers from the old list.
                r = put(n["Country"], "India")
                check("Country set; the State row was inserted under it",
                      r.ok and page.evaluate("()=>!!document.getElementById('10941')"), r.note)
                r = put(n["Education Level"], "Bachelor's Degree")
                check("old #Education Level: found again by signature and set",
                      r.ok and val("10959") == "Bachelor's Degree", r.note)
                check("...reports its new number and tells the agent numbers moved",
                      r.moved_to == n["Education Level"] + 1
                      and f.FIELD_MOVED_NOTE + str(r.moved_to) in r.as_agent_message(),
                      f"{r.moved_to} {r.as_agent_message()[:200]}")
                r = put(n["Job Title"], "Software Engineer")
                check("old #Job Title: set on the real Job Title box", r.ok and val("10979-1-0") == "Software Engineer",
                      r.note)
                n2 = load()  # fresh page, fresh listing
                no_idx = [x.index for x in f.list_fields(cdp).fields if x.kind == "radio"][1]
                put(n2["Country"], "India")
                r = put(no_idx, "true")
                check("old # of the passport 'No' radio: the No button is checked, not Yes",
                      r.ok and page.evaluate("()=>document.getElementById('10985-n').checked "
                                             "&& !document.getElementById('10985-y').checked"),
                      r.note)
                r = put(n2["What is the name of your college"], "Other")
                r = put(n2["Education Level"], "Master's Degree")
                check("two inserted rows above it: still the right control",
                      r.ok and val("10959") == "Master's Degree" and r.moved_to == n2["Education Level"] + 2,
                      f"{r.note} moved_to={r.moved_to}")

                # 2. A control that is really gone is refused, nothing typed.
                n = load()
                page.evaluate("()=>{const i=document.getElementById('10979-1-0');"
                              "i.id='x'; i.name='x'; i.previousElementSibling.textContent='Employer';}")
                r = put(n["Job Title"], "Software Engineer")
                check("replaced control: refused as stale, nothing typed",
                      not r.ok and r.stale and agent_driver.FORM_CHANGED_NOTE in r.note
                      and page.evaluate("()=>document.getElementById('x').value") == "", r.note)

                # 3. Two controls with the listed signature: no guessing.
                n = load()
                page.evaluate("()=>{const d=document.getElementById('dupes');"
                              "d.insertAdjacentHTML('beforebegin', d.outerHTML);"
                              "document.getElementById('10940').closest('.row').insertAdjacentHTML("
                              "'afterend','<div class=row><input name=pad></div>');}")
                r = put(n["Notes"], "hello")
                check("ambiguous signature after a shift: refused as stale",
                      not r.ok and r.stale and not any(page.evaluate(
                          "()=>[...document.getElementsByName('n')].map(e=>e.value)")), r.note)

                # 4. A listed number past the end of a shrunk form is stale too.
                n = load()
                page.evaluate("()=>document.getElementById('dupes').remove()")
                r = put(n["Notes"], "hello")
                check("listed field gone off the end: stale with the form-changed note",
                      not r.ok and r.stale and agent_driver.FORM_CHANGED_NOTE in r.note, r.note)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    driver_checks(check)
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


STALE = ("FIELD_SET_FAILED: the form changed since the fields were listed (#18 is now "
         "Education Level | 10959 | 10959). Re-list the fields and use the new number.")
VERDICT = "STATUS: submitted\nDETAIL: done\nFIELDS_FILLED_JSON: {}\n"


def _drive(rounds, replies):
    calls, sets, lists = [], [], []

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append(prompt)
        i = len(calls) - 1
        return (rounds[i] if i < len(rounds) else "session_id: s1\n" + VERDICT), False

    def resolve_field(line, *, cdp_url, job_id, failures):
        sets.append(line.strip())
        return replies.get(line.strip(), "FIELD_SET: ok")

    def listing(*, cdp_url, job_id):
        lists.append(1)
        return "FIELDS_LISTED: 3 control(s) on the active page:\n  #1 Fresh"

    names = ("_run_hermes", "_resolve_field", "_resolve_fields_listing", "_dump_debug_transcript",
             "session_api_calls")
    saved = [getattr(agent_driver, k) for k in names]
    for k, v in zip(names, (run_hermes, resolve_field, listing, lambda *a, **k: None, lambda *a, **k: 1)):
        setattr(agent_driver, k, v)
    try:
        out = agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK",
                                           cdp_url="http://127.0.0.1:1", job_id=1628,
                                           resume_path="/tmp/r.pdf")
    finally:
        for k, v in zip(names, saved):
            setattr(agent_driver, k, v)
    return out, calls, sets, lists


def driver_checks(check):
    turn = ("session_id: s1\nNEED_FIELD: #9 = India\nNEED_FIELD: #18 = No\n"
            "NEED_FIELD: #28 = Yes\nNEED_FIELD: #30 = No\n")
    full = "session_id: s1\n" + "".join(f"NEED_FIELD: #{i} = v\n" for i in range(1, 40))
    out, calls, sets, lists = _drive([turn, full], {"NEED_FIELD: #18 = No": STALE})
    check("driver: the batch stops at the stale set; later lines are not run",
          sets[:2] == ["NEED_FIELD: #9 = India", "NEED_FIELD: #18 = No"]
          and "NEED_FIELD: #28 = Yes" not in sets, str(sets[:4]))
    check("driver: the agent is told which lines were held back",
          "NOT RUN" in calls[1] and "#28 = Yes" in calls[1] and "#30 = No" in calls[1], calls[1][:300])
    check("driver: the form is re-listed in the same resume, no NEED_FIELDS round",
          len(lists) == 1 and "FIELDS_LISTED" in calls[1] and "new numbers" in calls[1], calls[1][-300:])
    check(f"driver: the stale set is not charged ({agent_driver.MAX_FIELD_ROUNDS - 1} sets left after 1 good + 1 stale)",
          len(sets) - 2 == agent_driver.MAX_FIELD_ROUNDS - 1, str(len(sets) - 2))

    out, calls, sets, lists = _drive(
        ["session_id: s1\nNEED_FIELD: #2 = a\nNEED_FIELD: #3 = b\n"],
        {"NEED_FIELD: #2 = a": "FIELD_SET: x = 'a' (via native)\n\nNote: "
                               + f.FIELD_MOVED_NOTE + "4; numbers after it may have moved too."})
    check("driver: a moved field still runs the rest of the batch, then re-lists",
          sets == ["NEED_FIELD: #2 = a", "NEED_FIELD: #3 = b"] and len(lists) == 1, f"{sets} {lists}")

    out, calls, sets, lists = _drive(["session_id: s1\nNEED_FIELD: #2 = a\n"], {})
    check("driver: an ordinary batch is not re-listed", not lists)

    many = "session_id: s1\n" + "NEED_FIELD: #18 = No\n"
    out, calls, sets, lists = _drive([many] * 40, {"NEED_FIELD: #18 = No": STALE})
    check("driver: refunds and re-lists are bounded for a form that never settles",
          len(lists) == agent_driver.MAX_STALE_REFUNDS
          and len(sets) == agent_driver.MAX_STALE_REFUNDS + agent_driver.MAX_FIELD_ROUNDS,
          f"lists={len(lists)} sets={len(sets)}")

    # _resolve_field: a stale result is no strike against the field.
    real = f.set_field
    f.set_field = lambda *a, **k: f.SetFieldResult(ok=False, stale=True, note=STALE[18:])
    try:
        fails: dict[str, int] = {}
        msgs = [agent_driver._resolve_field("NEED_FIELD: #18 = No", cdp_url="http://127.0.0.1:1",
                                            job_id=1, failures=fails) for _ in range(5)]
    finally:
        f.set_field = real
    check("_resolve_field: stale is not a strike (never ABANDONED)",
          not fails and not any("ABANDONED" in m for m in msgs), str(fails))


if __name__ == "__main__":
    sys.exit(main())
