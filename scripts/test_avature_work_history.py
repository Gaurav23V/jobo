#!/usr/bin/env python3
"""Southgate/Avature Work Experience step prefilled from a saved profile (GHO-8).

Live failure, Southgate 1528, 2026-10-14 (80/80 steps, session
20261014_112426_4d9d85 in the jobo profile's state.db): after the password
reset the agent signed in to the user's existing Avature account. The Work
Experience step loaded the SAVED profile, with Current Position, two Work
History entries (Sample Labs current, Test Systems) and Education filled. Every
required box had a value, so Continue would have worked. Prompt rule 4 ("re-verify
every prefilled field") sent it rewriting the entries to mirror the
candidate facts instead: retitling entry 1, ADD ANOTHER, typing Roles text,
dates, country. It used the last ~25 steps and never reached Submit. Southgate 1419
(2026-10-03) burned its budget on the same portal.

Fix under test:
  * FIELDS_LISTED ends with a required-field summary: either the required
    boxes still empty, or that every required box already has a value and
    prefilled history entries stay as they are.
  * The system prompt scopes rule 4 to single fields and tells the agent to
    leave repeating history sections alone, and the Avature note covers
    returning accounts (sign in first, Continue past the saved profile).

Runs against scripts/avature_work_history_probe.html. Self-isolated: own
headless Chromium on a random port, never 9333. Nothing on the probe
submits; the test also asserts nothing was clicked.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import form_filler as ff, prompts  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("avature_work_history_probe.html")

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def test_summary_unit():
    F = ff.FieldInfo
    filled = ff.ListFieldsResult(ok=True, total=3, fields=[
        F(index=1, label="Employer", kind="text", required=True, value="Sample Labs"),
        F(index=2, label="Roles", kind="textarea", value=""),  # optional, empty: fine
        F(index=3, label="Yes", kind="radio", required=True, checked=False, value="yes"),
    ])
    msg = filled.as_agent_message()
    check("unit: all required filled -> says so and says Continue",
          "Every required field listed above already has a value" in msg and "Continue" in msg, msg[-300:])
    check("unit: one unchecked radio of a group is not reported empty", "EMPTY" not in msg, msg[-300:])
    check("unit: summary sits before the NEED_FIELD how-to",
          msg.index("Every required field") < msg.index("Set one with its NUMBER"))

    gaps = ff.ListFieldsResult(ok=True, total=70, fields=[
        F(index=1, label="Employer", kind="text", required=True, value="  "),
        F(index=2, label="Country", kind="select", required=True, value=""),
        F(index=3, label="I Acknowledge *", kind="checkbox", required=True, checked=False, value="on"),
        F(index=4, label="City", kind="text", required=True, value="Springfield City"),
    ])
    s = gaps.required_summary()
    check("unit: empty text/select and unticked required checkbox are named",
          "#1 Employer" in s and "#2 Country" in s and "#3 I Acknowledge" in s and "#4" not in s, s)
    check("unit: says when the page has more controls than were listed", "first 4 of 70" in s, s)


def test_prompt():
    p = prompts.SYSTEM_PROMPT
    check("prompt: rule 4 is scoped to single fields, not history sections",
          "REPEATING history" in p and "Leave those entries exactly as they are" in p)
    check("prompt: Avature returning-account note says sign in first and Continue past the saved profile",
          "Returning accounts (Southgate, Wingtip, Kestrel" in p and "sign in first with Example-Passw0rd!" in p
          and "leave the history entries alone" in p)


def main() -> int:
    test_summary_unit()
    test_prompt()
    with tempfile.TemporaryDirectory(prefix="jobo-avature-wh-", ignore_cleanup_errors=True) as tmpd:
        profile = Path(tmpd) / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + str(profile),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = profile / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            with sync_playwright() as p:
                page = p.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
                state = lambda: page.evaluate("() => window.__state()")
                page.goto(PROBE.as_uri())

                # 1. The step exactly as the saved profile loaded it.
                ls = ff.list_fields(cdp)
                by = {}
                for f in ls.fields:
                    by.setdefault(f.label, []).append(f)
                employers = [f.value for f in by.get("Employer", [])]
                check("list_fields: both saved Work History entries listed with their values",
                      employers == ["Sample Labs", "Test Systems"], str(employers))
                check("list_fields: entry boxes are marked required",
                      all(f.required for f in by.get("Employer", []) + by.get("Position Title", [])),
                      str([(f.label, f.required) for f in ls.fields]))
                check("list_fields: total control count reported", ls.total == len(ls.fields), f"{ls.total}/{len(ls.fields)}")
                msg = ls.as_agent_message()
                check("prefilled step: agent is told every required field has a value",
                      "Every required field listed above already has a value" in msg and "EMPTY" not in msg,
                      msg[-600:])
                check("prefilled step: agent is told to leave history entries and Continue",
                      "do not rewrite them, click Continue" in msg, msg[-400:])

                # 2. A saved entry with a required box missing: named, nothing else.
                page.fill("#wh2_employer", "")
                ls2 = ff.list_fields(cdp)
                s2 = ls2.required_summary()
                emp2 = next(f for f in ls2.fields if f.label == "Employer" and not f.value)
                check("one required box emptied: exactly that box is named",
                      f"#{emp2.index} Employer" in s2 and s2.count("#") == 1, s2)

                s = state()
                check("listing clicked nothing: no Continue, ADD ANOTHER or REMOVE",
                      s["continueClicks"] == 0 and s["addClicks"] == 0 and s["removeClicks"] == 0, str(s))
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
