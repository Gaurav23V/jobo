#!/usr/bin/env python3
"""Long forms: list past the 60-control cap, and budget for the length (GHO-107).

Live failures (run 2026-10-30 22:00): both jobs printed NEED_FIELDS, got
"60 field(s)" back and ran out of steps mid-form with no NEED_FIELD set at all.
The 60 was the listing cap, not the form:
  * Liberty 3048 (Paylocity, retry) had 91 controls: three work history blocks
    filled the list, so the agent typed the rest by hand and ran out at 50/50
    (the retry budget) on step 2 of 4.
  * Consolidated 2802 (LinkedIn Easy Apply) had 209: the list was 60
    nationality checkboxes named "_r_g1_".."_r_jh_" (the option text sits in a
    sibling div), and the CTC / notice / right-to-work questions after them
    were never listed. It spent 30 console calls finding "Indian" and ran out
    at 80/80.

Covers:
  * in the page: `list_fields(start=61)` lists the next page with the same
    numbers, the first page says how to get it, and a number from either page
    sets the right control (page-1 numbers still checked after page 2 is read)
  * a checkbox whose option text sits in a sibling wrapper lists by that text,
    and can be ticked by number
  * in the driver: `NEED_FIELDS #61` asks for that page; a listing of 40+
    controls adds LONG_FORM_STEPS once, lifts a retry to the first-try budget
    first, extends the time, and tells the agent to set fields by number

Self-isolated: own headless Chromium on a random port, never 9333. The driver
part is offline (no browser, no model calls).
"""
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import agent_driver, form_filler as f, runner  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def history_block(n: int) -> str:
    # Paylocity's work history entry: 15 controls each.
    return "".join([
        f'<div class=wh><label for="co{n}">Company Name (required)</label><input id="co{n}" required>',
        f'<label for="po{n}">Position (required)</label><input id="po{n}" required>',
        f'<label for="cw{n}">Company Website</label><input id="cw{n}">',
        f'<label for="cp{n}">Company Phone Number</label><input id="cp{n}">',
        f'<label for="re{n}">Responsibilities (required)</label><input id="re{n}" required>',
        f'<label for="ct{n}">Country</label><select id="ct{n}"><option value=""></option>'
        '<option>India</option><option>United States</option></select>',
        f'<label for="a1{n}">Address Line 1</label><input id="a1{n}">',
        f'<label for="a2{n}">Address Line 2(optional)</label><input id="a2{n}">',
        f'<label for="lo{n}">Locality</label><input id="lo{n}">',
        f'<label for="ad{n}">Administrative Area</label><input id="ad{n}">',
        f'<label for="pc{n}">Postal Code</label><input id="pc{n}">',
        f'<label for="sd{n}">Start date</label><input id="sd{n}" placeholder="MM/YYYY">',
        f'<label for="ed{n}">End date</label><input id="ed{n}" placeholder="MM/YYYY">',
        f'<label for="rl{n}">Reason for Leaving (required)</label><input id="rl{n}" required>',
        f'<label><input type=checkbox id="cur{n}"> Currently Working Here</label></div>',
    ])


# 16 personal controls + 5 history blocks (75) = 91, as on 3048.
PAYLOCITY = ("<!doctype html><html><body><form>"
             + "".join(f'<label for="p{i}">Personal {i}</label><input id="p{i}">' for i in range(1, 15))
             + '<label for="docker">Docker</label><input id="docker">'
             + '<label for="cover">Cover letter</label><input id="cover" type=file>'
             + "".join(history_block(n) for n in range(1, 6))
             + "</form></body></html>")

NATIONALITIES = ["Afghan", "Albanian", "Algerian"] + [f"Nation{i:03d}" for i in range(1, 75)] + ["Indian"] \
    + [f"Nation{i:03d}" for i in range(75, 190)]
# LinkedIn's checkbox list: no <label for>, no aria name; text in a sibling div.
LINKEDIN = ("<!doctype html><html><body><div role=dialog><form><fieldset><legend>Nationality</legend>"
            + "".join(f'<div class=opt><div><input type=checkbox id="_r_{i}_"></div>'
                      f'<div><p>{n}</p></div></div>' for i, n in enumerate(NATIONALITIES))
            + "</fieldset>"
            + '<input id="ctc" type=text required aria-label="Please advise your current fixed CTC">'
            + '<input id="ectc" type=text required aria-label="Please advise on your expected fixed CTC">'
            + '<select id="np" aria-label="Notice period"><option value=""></option>'
            '<option>Immediate</option><option>30 days</option></select>'
            + "</form></div></body></html>")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def browser_part() -> None:
    with tempfile.TemporaryDirectory(prefix="jobo-gho107-", ignore_cleanup_errors=True) as profile:
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
                put = lambda i, v: f.set_field(cdp, f"#{i}", v, index=i)

                print("\n[1] Liberty 3048's Paylocity shape: 91 controls")
                html = Path(profile) / "paylocity.html"
                html.write_text(PAYLOCITY)
                page.goto(html.as_uri())
                one = f.list_fields(cdp)
                f.remember_listing(cdp, one)
                msg = one.as_agent_message()
                check("page 1 lists #1-#60 and knows the total",
                      [x.index for x in one.fields] == list(range(1, 61)) and one.total == 91,
                      f"{len(one.fields)} {one.total}")
                check("page 1 tells the agent how to get the rest",
                      "first 60 of 91 controls" in msg and "NEED_FIELDS #61" in msg, msg[-400:])
                check("...and not to click Continue before it has",
                      "click Continue / Next." not in msg, msg[-400:])
                two = f.list_fields(cdp, start=61)
                f.remember_listing(cdp, two)
                msg2 = two.as_agent_message()
                check("page 2 lists #61-#91 under the same numbers",
                      [x.index for x in two.fields] == list(range(61, 92)), str([x.index for x in two.fields][:3]))
                check("page 2 has the last block's Reason for Leaving",
                      any(x.label.startswith("Reason for Leaving") and x.index == 90 for x in two.fields),
                      str([(x.index, x.label) for x in two.fields][-3:]))
                check("the last page offers no further page", "NEED_FIELDS #" not in msg2, msg2[-300:])
                r = put(90, "Contract ended")
                check("#90 from page 2 sets the 5th block's Reason for Leaving",
                      r.ok and val("rl5") == "Contract ended", r.note)
                r = put(17, "Example Co")
                check("#17 from page 1 still sets after page 2 was listed",
                      r.ok and val("co1") == "Example Co", r.note)
                again = f.list_fields(cdp)
                f.remember_listing(cdp, again)
                check("a fresh listing from #1 forgets page 2's numbers",
                      f.listed_field(cdp, 90) is None and f.listed_field(cdp, 17) is not None)

                print("\n[2] Consolidated 2802's LinkedIn shape: 192 checkboxes, then the questions")
                html = Path(profile) / "linkedin.html"
                html.write_text(LINKEDIN)
                page.goto(html.as_uri())
                one = f.list_fields(cdp)
                f.remember_listing(cdp, one)
                check("checkboxes list by their option text, not their id",
                      one.fields[0].label == "Afghan" and not any(x.label.startswith("_r_") for x in one.fields),
                      str([x.label for x in one.fields[:3]]))
                total = len(NATIONALITIES) + 3
                pages, start = [], 1
                while start <= total:
                    ls = f.list_fields(cdp, start=start)
                    f.remember_listing(cdp, ls)
                    pages.append(ls)
                    start = ls.fields[-1].index + 1
                listed = {x.label: x.index for ls in pages for x in ls.fields}
                check("every control is reachable by paging", len(listed) == total and len(pages) == 4,
                      f"{len(listed)} of {total} in {len(pages)} pages")
                check("the CTC question past the nationalities is listed",
                      listed.get("Please advise your current fixed CTC") == total - 2, str(listed.get(
                          "Please advise your current fixed CTC")))
                r = put(listed["Please advise your current fixed CTC"], "7 LPA")
                check("...and set by its number", r.ok and val("ctc") == "7 LPA", r.note)
                r = put(listed["Indian"], "true")
                check("the 'Indian' box is ticked by number",
                      r.ok and page.evaluate("()=>document.getElementById('_r_77_').checked"), r.note)
        finally:
            proc.kill()
            proc.wait()


SUBMITTED = "STATUS: submitted\nDETAIL: confirmation page shown\nFIELDS_FILLED_JSON: {}"


def drive(rounds, counts, listings, *, is_retry=False, budget_boost=False):
    calls, list_starts = [], []
    outs, steps, lists = iter(rounds), iter(counts), iter(listings)

    def fake_run(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append({"max_turns": int(max_turns), "timeout": timeout_sec, "prompt": prompt})
        return next(outs) + "\n\nsession_id: s1\n", False

    def fake_list(*, cdp_url, job_id, start=1):
        list_starts.append(start)
        return next(lists)

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", side_effect=lambda sid, p: next(steps)))
        st.enter_context(patch.object(agent_driver, "_resolve_field",
                                      return_value="FIELD_SET: Question = 'x' (via text)"))
        st.enter_context(patch.object(agent_driver, "_resolve_upload", return_value="UPLOAD_DONE: attached"))
        st.enter_context(patch.object(agent_driver, "_resolve_fields_listing", side_effect=fake_list))
        st.enter_context(patch.object(agent_driver, "_seen_workday", return_value=False))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": ""}))
        out = agent_driver.run_apply_agent(
            system_prompt="S", user_prompt="U", cdp_url="http://x", job_id=3048,
            resume_path="/r.pdf", is_retry=is_retry, budget_boost=budget_boost)
    return out, calls, list_starts


PAGE1 = "FIELDS_LISTED: 60 control(s) on the active page:\n ...\nOnly the first 60 of 91 controls are listed."
PAGE2 = "FIELDS_LISTED: 31 control(s) on the active page:\n ...\nThis list shows #61-#91 of 91 controls."
SHORT = "FIELDS_LISTED: 12 control(s) on the active page:\n ..."


def driver_part() -> None:
    first = agent_driver.AGENT_STEPS_FIRST
    retry = agent_driver.AGENT_STEPS_RETRY
    extra = agent_driver.LONG_FORM_STEPS

    print("\n[3] Liberty 3048's retry: a 91-control listing lifts the retry budget, once")
    rounds = ["NEED_UPLOAD: resume", "NEED_FIELDS", "NEED_FIELDS #61",
              "NEED_FIELD: #90 = Contract ended", SUBMITTED]
    out, calls, starts = drive(rounds, [6, 20, 22, 24, 70], [PAGE1, PAGE2], is_retry=True)
    budget = first + extra
    check("the retry starts on the retry budget", calls[0]["max_turns"] == retry, str(calls[0]["max_turns"]))
    check("`NEED_FIELDS #61` asks for the page from #61", starts == [1, 61], str(starts))
    check("after the long listing the budget is first-try + LONG_FORM_STEPS",
          calls[2]["max_turns"] == budget - 20, str(calls[2]["max_turns"]))
    check("the agent is told its new total and to set by number",
          f"now {budget}" in calls[2]["prompt"] and "text boxes too" in calls[2]["prompt"]
          and "ONE turn" in calls[2]["prompt"], calls[2]["prompt"][-300:])
    check("granted once: the second page adds nothing",
          sum("controls, so" in c["prompt"] for c in calls) == 1
          and calls[3]["max_turns"] == budget - 22, str(calls[3]["max_turns"]))
    check("the retry's time grows past the 8-minute retry cap",
          calls[2]["timeout"] > agent_driver.AGENT_TIMEOUT_RETRY_SEC, str(calls[2]["timeout"]))
    check("the job can use steps past the old 50 and submit",
          out.status == "submitted", f"{out.status}: {out.detail}")

    print("\n[4] Consolidated 2802's first try: +LONG_FORM_STEPS, no lift")
    out, calls, _ = drive(["NEED_FIELDS", SUBMITTED], [30, 95],
                          ["FIELDS_LISTED: 60 control(s)\nOnly the first 60 of 209 controls are listed."])
    check("first try goes to first-try + LONG_FORM_STEPS",
          calls[1]["max_turns"] == first + extra - 30, str(calls[1]["max_turns"]))
    check("...and submits past step 80", out.status == "submitted", out.detail)

    print("\n[5] a short form gets nothing; a long one still has a hard cap")
    out, calls, _ = drive(["NEED_FIELDS", SUBMITTED], [10, 40], [SHORT])
    check("12 controls: no allowance", calls[1]["max_turns"] == first - 10
          and "controls, so" not in calls[1]["prompt"], str(calls[1]["max_turns"]))
    out, calls, _ = drive(["NEED_FIELDS", "NEED_FIELD: #3 = x"], [10, first + extra], [PAGE1])
    check("out of the long-form budget: stuck, base number kept, allowance named",
          out.status == "stuck" and f"({first} model steps)" in out.detail
          and f"plus {extra} for a long form" in out.detail, out.detail)
    check("runner reads it as budget exhaustion (the next retry escalates)",
          runner._is_budget_exhaustion(f"stuck:{out.detail}"))
    esc = agent_driver.AGENT_STEPS_ESCALATED
    out, calls, _ = drive(["NEED_FIELDS", SUBMITTED], [10, 60], [PAGE1], is_retry=True, budget_boost=True)
    check("an escalated retry is not lowered: escalated + LONG_FORM_STEPS",
          calls[1]["max_turns"] == esc + extra - 10, str(calls[1]["max_turns"]))
    check("listing totals parse", agent_driver._listed_total(PAGE1) == 91
          and agent_driver._listed_total(SHORT) == 12 and agent_driver._listed_total("UPLOAD_DONE") == 0)


def main() -> int:
    browser_part()
    driver_part()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
