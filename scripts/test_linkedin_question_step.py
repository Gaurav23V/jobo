#!/usr/bin/env python3
"""LinkedIn Easy Apply question step: radio options, field errors, CTC digits (GHO-112).

Live failure (retry 2026-10-31 14:27): Consolidated 2802 reached the last
LinkedIn step and still used 130/130 steps.
  * Each Yes/No question listed both radios under the question text with
    option_value='on'. `#188 = Yes` was refused, `#188 = true` failed with
    "exact widget identity changed" (LinkedIn re-mounts the radio it checks,
    _r_ql_ -> _r_ro_) although the check landed, and the agent guessed which
    number meant Yes on a right-to-work question.
  * Next showed "Invalid input" under both CTC boxes ("7 LPA"; the box takes
    700000) and the listing never said so: ~60 steps of console scripts.

Fixture: scripts/fixtures/linkedin_easyapply_questions.html, the step's DOM
as probed on 2026-10-31, served under a linkedin.com URL.

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

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "linkedin_easyapply_questions.html"
URL = "https://www.linkedin.com/jobs/view/0/apply/"
RIGHT_TO_WORK = "Do you currently have full right to work in India"
COMMUTE = "Are you based within a commutable distance"

# A classic form: the label is the option, and a hint is not an error.
CLASSIC = ("<!doctype html><html><body><form><fieldset><legend>Relocate?</legend>"
           "<label><input type=radio name=rel value=Yes> Yes</label>"
           "<label><input type=radio name=rel value=No> No</label></fieldset>"
           '<label for=sal>Expected salary</label><input id=sal aria-describedby=salh>'
           '<div id=salh>Annual figure, any format</div>'
           '<label for=dob>Date of birth</label><input id=dob aria-describedby=dobh>'
           '<div id=dobh>Use DD/MM/YYYY</div></form></body></html>')

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def amounts() -> None:
    print("\n[1] bare_amount: the digits a numbers-only salary box wants")
    cases = [("7 LPA", "", "700000"), ("₹7,00,000", "", "700000"), ("7.5 lakh", "", "750000"),
             ("1.2 Cr", "", "12000000"), ("INR 700k", "", "700000"), ("7 LPA", "Current CTC (in LPA)", "7"),
             ("700000", "", None), ("30 days", "", None), ("7", "", None), ("700000", "CTC in lakhs", None)]
    for value, label, want in cases:
        got = f.bare_amount(value, label)
        check(f"{value!r} (label {label!r}) -> {want!r}", got == want, repr(got))


def browser_part() -> None:
    with tempfile.TemporaryDirectory(prefix="jobo-gho112-", ignore_cleanup_errors=True) as profile:
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
                page.route("https://www.linkedin.com/**", lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text()))
                page.goto(URL)

                def listing():
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    return ls

                def find(ls, start, option=None):
                    return next(x for x in ls.fields if x.label.startswith(start)
                                and (option is None or x.option == option))

                checked = lambda q, opt: page.evaluate(
                    """([q, opt]) => [...document.querySelectorAll('input[type=radio]')]
                         .filter(r => r.getAttribute('aria-label').startsWith(q) &&
                                      r.closest('div').parentElement.innerText.trim() === opt)
                         .map(r => r.checked)""", [q, opt])

                print("\n[2] Each radio lists its own option, under its question")
                ls = listing()
                msg = ls.as_agent_message()
                yes, no = find(ls, RIGHT_TO_WORK, "Yes"), find(ls, RIGHT_TO_WORK, "No")
                check("right-to-work lists one 'Yes' and one 'No' radio",
                      yes.kind == no.kind == "radio" and yes.index != no.index,
                      str([(x.index, x.option) for x in ls.fields if x.kind == "radio"]))
                check("...shown as option='Yes' on the listing line",
                      f"#{yes.index}" in msg and "option='Yes'" in yes.as_line(), yes.as_line())
                check("...without the meaningless option_value='on'", "option_value='on'" not in msg, msg[:600])
                check("nationality boxes still list by their own text, no option= repeat",
                      find(ls, "Afghan").option == "" and "option=" not in find(ls, "Afghan").as_line())
                check("every radio has a distinct signature",
                      len({x.sig for x in ls.fields if x.kind == "radio"}) == 6)

                print("\n[3] Setting a radio by its option text")
                r = f.set_field(cdp, f"#{yes.index}", "Yes", index=yes.index)
                check("#Yes = 'Yes' succeeds although LinkedIn re-mounted the radio",
                      r.ok and "replaced-node" in r.strategy, f"{r.note} {r.strategy}")
                check("...and Yes is the checked one", checked(RIGHT_TO_WORK, "Yes") == [True]
                      and checked(RIGHT_TO_WORK, "No") == [False],
                      str(checked(RIGHT_TO_WORK, "Yes")))
                c_yes, c_no = find(ls, COMMUTE, "Yes"), find(ls, COMMUTE, "No")
                r = f.set_field(cdp, f"#{c_yes.index}", "No", index=c_yes.index)
                check("'No' on the Yes radio is refused and names the option",
                      not r.ok and '"Yes" option' in r.note and checked(COMMUTE, "Yes") == [False], r.note)
                r = f.set_field(cdp, f"#{c_no.index}", "No", index=c_no.index)
                check("the No radio from the same (old) listing sets 'No'",
                      r.ok and checked(COMMUTE, "No") == [True], r.note)
                r = f.set_field(cdp, f"#{c_no.index}", "true", index=c_no.index)
                check("its number still holds after the re-mount ('true' again)", r.ok, r.note)

                print("\n[4] The form's own errors reach the listing")
                ctc = find(ls, "Please advise your current fixed CTC")
                ectc = find(ls, "Please advise on your expectations")
                notice = find(ls, "Please advise your notice period")
                check("CTC boxes list as numbers only, notice period does not",
                      ctc.numeric and ectc.numeric and not notice.numeric, f"{ctc.numeric} {notice.numeric}")
                # What the agent did by hand on 2802: the unit form, typed in.
                page.fill("#_r_r6_", "7 LPA")
                page.click("#next")
                ls = listing()
                msg = ls.as_agent_message()
                ctc = find(ls, "Please advise your current fixed CTC")
                check("the CTC box lists error='Invalid input'", ctc.error == "Invalid input", ctc.as_line())
                check("...and the bare number to enter instead",
                      "enter the bare number 700000" in ctc.as_line(), ctc.as_line())
                check("the character counter is not an error", "/20" not in ctc.error and "characters" not in ctc.error)
                hybrid = [x for x in ls.fields if x.label.startswith("In Consolidated we operate")]
                check("an unanswered question's radios list 'This field is required'",
                      hybrid and all(x.error == "This field is required" for x in hybrid), str([x.error for x in hybrid]))
                check("an answered question has no error", find(ls, RIGHT_TO_WORK, "Yes").error == "")
                check("the summary names the flagged fields",
                      "The form shows an error under:" in msg and f"#{ctc.index} 'Invalid input'" in msg, msg[-700:])

                print("\n[5] Numbers-only boxes get the bare amount")
                r = f.set_field(cdp, f"#{ctc.index}", "7 LPA", index=ctc.index)
                check("'7 LPA' is entered as 700000, and the agent is told",
                      r.ok and page.input_value("#_r_r6_") == "700000" and "numbers only" in r.as_agent_message(),
                      f"{r.note} {page.input_value('#_r_r6_')}")
                r = f.set_field(cdp, f"#{ectc.index}", "9 LPA", index=ectc.index)
                check("expected CTC '9 LPA' -> 900000", r.ok and page.input_value("#_r_r7_") == "900000", r.note)
                r = f.set_field(cdp, f"#{notice.index}", "30 days", index=notice.index)
                check("notice period keeps '30 days'", r.ok and page.input_value("#_r_r8_") == "30 days", r.note)
                hy = find(ls, "In Consolidated we operate", "Yes")
                r = f.set_field(cdp, f"#{hy.index}", "Yes", index=hy.index)
                nat = find(ls, "Albanian")
                r2 = f.set_field(cdp, f"#{nat.index}", "true", index=nat.index)
                page.click("#next")
                check("with every answer set by number, Next advances",
                      r.ok and r2.ok and page.evaluate("document.body.dataset.page") == "advanced",
                      f"{r.note} {r2.note} {page.evaluate('document.body.dataset.page')}")

                print("\n[6] Classic forms are unchanged")
                html = Path(profile) / "classic.html"
                html.write_text(CLASSIC)
                page.goto(html.as_uri())
                ls = listing()
                radios = [x for x in ls.fields if x.kind == "radio"]
                check("label-wrapped radios list by their label, no option= repeat",
                      [x.label for x in radios] == ["Yes", "No"] and all(not x.option for x in radios),
                      str([(x.label, x.option) for x in radios]))
                check("...and keep their real value", all(f"option_value='{x.label}'" in x.as_line() for x in radios))
                sal, dob = find(ls, "Expected salary"), find(ls, "Date of birth")
                check("a describedby hint is not an error", sal.error == "" and dob.error == "", f"{sal.error} {dob.error}")
                check("a salary box off LinkedIn with no digit cue is free text", not sal.numeric)
                r = f.set_field(cdp, f"#{sal.index}", "7 LPA", index=sal.index)
                check("...and keeps '7 LPA'", r.ok and r.readback == "7 LPA", r.note)
                r = f.set_field(cdp, f"#{radios[1].index}", "No", index=radios[1].index)
                check("'No' still sets a label-wrapped No radio", r.ok, r.note)
        finally:
            proc.kill()
            proc.wait()


def main() -> int:
    amounts()
    browser_part()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
