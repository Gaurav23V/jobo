#!/usr/bin/env python
"""A long form that is still moving gets one more step allowance (GHO-89).

Wingtip 1594 (Avature, 2026-10-24) uploaded its files on two wizard pages, set
17 fields through NEED_FIELD and reached the per-country work authorization
page, then ran out at 80/80 right after setting "legal authorization to work
in Mexico". The other three Wingtip jobs in that run did the same form in 33-48
steps. The handoff sequence below is 1594's, from the run log.

Covers:
  * new fields set in the last steps grant PROGRESS_EXTENSION_STEPS and
    PROGRESS_EXTENSION_SEC once, and the agent is told its new total
  * the grant comes at the low-water mark, before Hermes runs dry mid-run
  * re-setting the same field, or fields set long ago, grants nothing
  * the grant is one-off: still moving at the new cap still stops
  * the out-of-budget detail keeps the BASE budget number, so the runner and
    blockers.py read it as before; account setup still adds its own allowance

Offline: no browser, no Gmail, no model calls, no subprocesses.
"""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4 import agent_driver, blockers, form_filler, runner  # noqa: E402

PASS = 0
FAIL = 0

BASE = agent_driver.AGENT_STEPS_FIRST
EXTRA = agent_driver.PROGRESS_EXTENSION_STEPS
SUBMITTED = "STATUS: submitted\nDETAIL: confirmation page shown\nFIELDS_FILLED_JSON: {}"

# 1594's handoffs from run_20261024_1000.log: (agent output, form control set).
WINGTIP_1594 = [
    ("NEED_UPLOAD: resume | \"From Device\" button under \"Upload Resume\"", None),
    ("NEED_FIELDS", None),
    ("NEED_FIELD: #8 = Country A", "Country * | 6355 | 6355"),
    ("NEED_FIELD: #14 = Bachelor", "Highest level of education * | 6363-25-0 | 6363-25-0"),
    ("NEED_FIELD: #15 = Example Institute", "School * | 6363-8-0 | 6363-8-0"),
    ("NEED_FIELD: #9 = Region A", "State/Province | 6356 | 6356"),
    ("NEED_FIELD: #16 = Field A", "Field of study | 6363-11-0 | 6363-11-0"),
    ("NEED_FIELD: #23 = Industry A", "Company Industry * | 6366-7-0 | 6366-7-0"),
    ("NEED_FIELD: #24 = Function A", "Position Function * | 6366-12-0 | 6366-12-0"),
    ("NEED_FIELD: #29 = Industry B", "Company Industry * | 6366-7-1 | 6366-7-1"),
    ("NEED_FIELD: #30 = Function A", "Position Function * | 6366-12-1 | 6366-12-1"),
    ("NEED_FIELD: #33 = No", "Have you previously been employed at Wingtip? * | 6370 | 6370"),
    ("NEED_FIELD: #38 = LinkedIn", "How did you hear about Wingtip? * | 8770 | 8770"),
    ("NEED_UPLOAD: cover | button \"Cover Letter\" under \"Additional Documents\"", None),
    ("NEED_FIELDS", None),
    ("NEED_FIELD: #38 = LinkedIn", "How did you hear about Wingtip? * | 8775 | 8775"),
    ("NEED_UPLOAD: resume | button \"Resume\" (No file chosen)", None),
    ("NEED_UPLOAD: cover | button \"Cover Letter\" (No file chosen)", None),
    ("NEED_FIELDS", None),
    ("NEED_FIELD: #3 = No", "Are you currently authorized to work in the United States? * | 6404 | 6404"),
    ("NEED_FIELD: #4 = Yes", "Do you now or will you in the future require sponsorship? * | 6405 | 6405"),
    ("NEED_FIELD: #5 = No", "Are you legally entitled to work in Canada? * | 6407 | 6407"),
    ("NEED_FIELD: #6 = Foreigner", "Singapore work visa need * | 6408 | 6408"),
    ("NEED_FIELD: #7 = No", "Do you have legal authorization to work in Mexico? * | 7838 | 7838"),
]
# Steps used after each round: front-loaded like the real session (36 total
# messages by the first field set), one or two steps per handoff at the end.
WINGTIP_1594_STEPS = [6, 14, 26, 28, 30, 34, 40, 42, 44, 46, 48, 50, 52, 56, 60,
                   64, 68, 70, 72, 74, 76, 77, 78, 80]


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def drive(rounds, counts, *, budget_boost: bool = False):
    """Run the driver against scripted agent rounds; return (outcome, calls).

    `rounds` is a list of (agent output, label the pipeline set or None).
    """
    calls = []
    outs = iter(rounds)
    steps = iter(counts)
    labels = {}

    def fake_run(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append({"max_turns": int(max_turns), "timeout": timeout_sec, "prompt": prompt})
        out, label = next(outs)
        if label:
            labels[out] = label
        return out + "\n\nsession_id: s1\n", False

    def fake_field(line, *, cdp_url, job_id, failures):
        return (f"FIELD_SET: {labels[line.strip()]} = 'x' (via select)\n\n"
                "The value is set and verified in the DOM.")

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", side_effect=lambda sid, p: next(steps)))
        st.enter_context(patch.object(agent_driver, "_resolve_field", side_effect=fake_field))
        st.enter_context(patch.object(agent_driver, "_resolve_upload", return_value="UPLOAD_DONE: attached"))
        st.enter_context(patch.object(agent_driver, "_resolve_fields_listing", return_value="FIELDS_LISTED: 12"))
        st.enter_context(patch.object(agent_driver, "_resolve_click", return_value="CLICK_DONE: page changed"))
        st.enter_context(patch.object(agent_driver, "_seen_workday", return_value=False))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": ""}))
        out = agent_driver.run_apply_agent(
            system_prompt="S", user_prompt="U", cdp_url="http://x", job_id=1594,
            resume_path="/r.pdf", cover_path="/c.pdf", is_retry=budget_boost,
            budget_boost=budget_boost,
        )
    return out, calls


def test_wingtip_1594_gets_room_to_finish():
    print("\n[1] Wingtip 1594: still setting new fields at the cap -> one allowance, then submits")
    rest = [("NEED_FIELD: #8 = No", "Mexico sponsorship * | 7839 | 7839"),
            ("NEED_CLICK: Submit", None), (SUBMITTED, None)]
    out, calls = drive(WINGTIP_1594 + rest, WINGTIP_1594_STEPS + [84, 88, 92])
    granted = [i for i, c in enumerate(calls) if "still moving" in c["prompt"]]
    check("granted exactly once", len(granted) == 1, str(granted))
    first = granted[0] if granted else -1
    check("granted at the low-water mark, before the budget ran dry",
          first > 0 and WINGTIP_1594_STEPS[first - 1] >= BASE - agent_driver.PROGRESS_LOW_WATER
          and WINGTIP_1594_STEPS[first - 1] < BASE, str(first))
    check("that round runs with the extra steps",
          first > 0 and calls[first]["max_turns"] == BASE + EXTRA - WINGTIP_1594_STEPS[first - 1],
          str(calls[first]["max_turns"]) if first > 0 else "")
    check("time is extended too",
          first > 0 and calls[first]["timeout"] > agent_driver.AGENT_TIMEOUT_FIRST_SEC,
          str(calls[first]["timeout"]) if first > 0 else "")
    check("the agent is told its new total", first > 0 and f"now {BASE + EXTRA}" in calls[first]["prompt"])
    check("and to batch its fields", first > 0 and "SAME turn" in calls[first]["prompt"])
    check("the rounds after 80 steps ran", len(calls) == len(WINGTIP_1594) + len(rest), f"{len(calls)} rounds")
    check("the job submits", out.status == "submitted", f"{out.status}: {out.detail}")


def test_same_field_loop_grants_nothing():
    print("\n[2] an agent re-setting one field near the cap gets nothing")
    loop = [("NEED_FIELD: #4 = Yes", "Sponsorship * | 6405 | 6405")] * 8
    out, calls = drive(loop, [40, 66, 70, 72, 74, 76, 78, BASE])
    check("no allowance note", not any("still moving" in c["prompt"] for c in calls))
    check("stops at the base budget", out.status == "stuck" and f"({BASE} model steps)" in out.detail, out.detail)
    check("detail has no progress suffix", "progressing" not in out.detail, out.detail)


def test_old_progress_grants_nothing():
    print("\n[3] fields set long before the cap, then nothing new, get nothing")
    early = [(f"NEED_FIELD: #{i} = x", f"Question {i} | {i} | {i}") for i in range(1, 6)]
    tail = [("NEED_CLICK: Next", None), ("NEED_CLICK: Next", None), ("NEED_CLICK: Next", None),
            ("NEED_CLICK: Next", None)]
    out, calls = drive(early + tail, [10, 12, 14, 16, 18, 50, 72, 76, BASE])
    check("no allowance note", not any("still moving" in c["prompt"] for c in calls))
    check("stops at the base budget", out.status == "stuck" and f"({BASE} model steps)" in out.detail, out.detail)


def test_hard_cap():
    print("\n[4] still moving at the extended cap: stops, and reads as budget exhaustion")
    rounds = [(f"NEED_FIELD: #{i} = x", f"Question {i} | {i} | {i}") for i in range(1, 12)]
    counts = [60, 66, 70, 72, 76, 84, 90, 96, 100, 104, BASE + EXTRA]
    out, calls = drive(rounds, counts)
    check("granted once only", sum("still moving" in c["prompt"] for c in calls) == 1)
    check("stuck at the new cap", out.status == "stuck" and len(calls) == len(rounds), f"{len(calls)}")
    check("detail keeps the base budget number", f"({BASE} model steps)" in out.detail, out.detail)
    check("detail names the progress allowance", f"plus {EXTRA} while still progressing" in out.detail, out.detail)
    check("runner reads it as budget exhaustion", runner._is_budget_exhaustion(f"stuck:{out.detail}"))


def test_escalated_and_account_setup():
    print("\n[5] escalated retry with account setup: both allowances, blockers still match")
    esc = agent_driver.AGENT_STEPS_ESCALATED
    acct = agent_driver.ACCOUNT_SETUP_STEPS
    rounds = [("NEED_CLICK: Create Account", None)] + [
        (f"NEED_FIELD: #{i} = x", f"Question {i} | {i} | {i}") for i in range(1, 9)]
    counts = [20, 110, 114, 118, 120, 126, 132, 140, esc + acct + EXTRA]
    out, calls = drive(rounds, counts, budget_boost=True)
    check("account setup granted", any("Account setup" in c["prompt"] for c in calls))
    check("progress granted after it", any("still moving" in c["prompt"] for c in calls))
    check("detail carries the escalated base number", f"({esc} model steps)" in out.detail, out.detail)
    check("detail names both allowances",
          f"plus {acct} for account setup" in out.detail and f"plus {EXTRA} while" in out.detail, out.detail)
    check("blockers' escalated-exhausted pattern still matches",
          bool(blockers._ESCALATED_BUDGET_STILL_EXHAUSTED.search(out.detail)), out.detail)


def test_label_parse():
    print("\n[6] the label comes from a real FIELD_SET reply, not a failure")
    ok = form_filler.SetFieldResult(ok=True, field_name="Country", matched="Country * | 6355 | 6355",
                                    readback="Country A", strategy="select").as_agent_message()
    bad = form_filler.SetFieldResult(ok=False, note="no such option").as_agent_message()
    check("success reply yields the control", agent_driver._FIELD_SET_LABEL_RE.findall(ok)
          == ["Country * | 6355 | 6355"], repr(ok[:80]))
    check("failure reply yields nothing", not agent_driver._FIELD_SET_LABEL_RE.findall(bad), repr(bad[:80]))


def main() -> int:
    test_wingtip_1594_gets_room_to_finish()
    test_same_field_loop_grants_nothing()
    test_old_progress_grants_nothing()
    test_hard_cap()
    test_escalated_and_account_setup()
    test_label_parse()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
