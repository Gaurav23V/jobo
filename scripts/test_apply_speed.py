"""GHO-15: cheaper and faster apply jobs, offline.

  1. click/scroll/press results carry the page after the action (plugin
     after_action.py), refs from before it are refused in the same response,
     and the guards/filter see that page like a navigate result;
  2. every NEED_FIELD line in a turn is resolved in ONE resume;
  3. post-submission pacing applies per apply host, not after every submit.

No browser, no model: the Hermes browser session and the hermes CLI are faked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from module4 import agent_driver, prompts, runner  # noqa: E402

PLUGIN_DIR = ROOT / "hermes_plugin" / "jobo-browser"
FAILS: list[str] = []
CHECKS = 0


def check(name, ok, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FORM = (
    '- heading "Apply for Backend Engineer" [ref=e1]\n'
    '- textbox "Full name" [ref=e2]\n'
    '- textbox "Password *" [ref=e3]\n'
    '- button "Submit application" [ref=e4]\n'
)
# Long enough (> MIN_FILTER_CHARS) that the snapshot filter rewrites it.
LONG_FORM = FORM + "".join(f'- paragraph\n  - StaticText "About us, part {i} ' + "x" * 80 + '"\n' for i in range(30))


def fake_snapshot(text=FORM):
    calls = []

    def snap(task_id):
        calls.append(task_id)
        return json.dumps({"success": True, "snapshot": text, "element_count": 4,
                           "frame_tree": {"top": {"url": "https://www.linkedin.com/jobs/view/1/"}}})
    return snap, calls


def test_after_action():
    aa = _load("jobo_after_action_test", PLUGIN_DIR / "after_action.py")
    aa.reset_state()
    snap, calls = fake_snapshot()
    probes = iter(["loading", "interactive", "complete"])
    slept = []
    out = aa.attach("browser_click", {"ref": "@e9"}, json.dumps({"success": True, "clicked": "@e9"}),
                    "t1", "turn1:api:3", snapshot=snap, ready_state=lambda t: next(probes), sleep=slept.append)
    data = json.loads(out or "{}")
    check("click result carries the page after the click, with fresh refs and a note",
          data.get("clicked") == "@e9" and "ref=e4" in data.get("snapshot", "")
          and data.get("jobo_page_after_action") == aa.NOTE and calls == ["t1"], str(data)[:200])
    check("waits for the document to finish loading first (polled, bounded)", len(slept) == 2, str(slept))
    check("supervisor fields (frame_tree of a possibly stale tab) are not merged in",
          "frame_tree" not in data and data.get("element_count") == 4, str(data.keys()))

    for tool in ("browser_scroll", "browser_press"):
        aa.reset_state()
        res = aa.attach(tool, {}, json.dumps({"success": True}), "t1", "r", snapshot=fake_snapshot()[0],
                        ready_state=lambda t: "complete")
        check(f"{tool} result carries the page too", res is not None and "ref=e2" in json.loads(res)["snapshot"])

    snap, calls = fake_snapshot()
    for tool, result in (
        ("browser_type", json.dumps({"success": True, "typed": "x"})),
        ("browser_snapshot", json.dumps({"success": True, "snapshot": FORM})),
        ("browser_click", json.dumps({"success": False, "error": "no such ref"})),
        ("browser_click", "not json"),
    ):
        check(f"left alone: {tool} {result[:30]}",
              aa.attach(tool, {"ref": "@e1"}, result, "t1", "r", snapshot=snap, ready_state=lambda t: "complete") is None)
    check("no snapshot taken for any of those", calls == [], str(calls))

    def boom(task_id):
        raise RuntimeError("daemon gone")
    check("a failing snapshot fails open (original result kept)",
          aa.attach("browser_click", {"ref": "@e1"}, json.dumps({"success": True}), "t1", "r",
                    snapshot=boom, ready_state=lambda t: "complete") is None)

    clock = [0.0]

    def tick(sec):
        clock[0] += sec
    aa.settle("t1", ready_state=lambda t: "loading", sleep=tick, clock=lambda: clock[0])
    check("a page that never finishes loading costs at most SETTLE_MAX_SEC",
          aa.SETTLE_MAX_SEC <= clock[0] <= aa.SETTLE_MAX_SEC + aa.SETTLE_POLL_SEC, str(clock[0]))

    os.environ["JOBO_AUTO_SNAPSHOT"] = "0"
    try:
        check("JOBO_AUTO_SNAPSHOT=0 turns it off",
              aa.attach("browser_click", {"ref": "@e1"}, json.dumps({"success": True}), "t1", "r",
                        snapshot=fake_snapshot()[0], ready_state=lambda t: "complete") is None)
    finally:
        del os.environ["JOBO_AUTO_SNAPSHOT"]

    aa.reset_state()
    aa.attach("browser_click", {"ref": "@e9"}, json.dumps({"success": True}), "t1", "turn1:api:3",
              snapshot=fake_snapshot()[0], ready_state=lambda t: "complete")
    check("same response, later ref-based call: refused (its refs are from the old page)",
          aa.stale_ref_block("browser_type", {"ref": "@e2", "text": "x"}, "t1", "turn1:api:3") == aa.STALE_REF_MESSAGE)
    check("next response: allowed", aa.stale_ref_block("browser_click", {"ref": "@e4"}, "t1", "turn1:api:4") is None)
    check("same response, call without a ref (press Enter): allowed",
          aa.stale_ref_block("browser_press", {"key": "Enter"}, "t1", "turn1:api:3") is None)
    check("another task's response: allowed",
          aa.stale_ref_block("browser_click", {"ref": "@e4"}, "t2", "turn1:api:3") is None)


def test_plugin_wiring():
    plugin = _load("jobo_browser_plugin_speed_test", PLUGIN_DIR / "__init__.py")
    aa = plugin.after_action
    aa.reset_state()
    plugin.account_guard.reset_state()
    plugin.account_guard._REF_NAMES.clear()
    saved = (aa._snapshot, aa._ready_state)
    aa._snapshot = fake_snapshot(LONG_FORM)[0]
    aa._ready_state = lambda t: "complete"
    try:
        out = plugin._transform_tool_result("browser_click", {"ref": "@e9"}, json.dumps({"success": True, "clicked": "@e9"}),
                                            task_id="t1", api_request_id="turn7:api:1")
        data = json.loads(out or "{}")
        snap = data.get("snapshot", "")
        check("plugin: click page goes through the snapshot filter (form view, prose dropped)",
              snap.startswith(plugin.snapshot_filter.MARKER) and "About us" not in snap and "ref=e4" in snap, snap[:160])
        check("plugin: the after-action note survives filtering", data.get("jobo_page_after_action") == aa.NOTE)
        check("plugin: account guard learned the new page's refs (password rewrite keeps working)",
              plugin.account_guard.is_password_ref("@e3"))
        blocked = plugin._pre_tool_call("browser_click", {"ref": "@e4"}, task_id="t1", api_request_id="turn7:api:1")
        check("plugin: a second ref-based call in the same response is blocked",
              (blocked or {}).get("action") == "block" and "same response" in blocked.get("message", ""))
        orig_scroll = plugin.click_scroller._run
        plugin.click_scroller._run = lambda *a, **k: {"success": True}
        try:
            ok = plugin._pre_tool_call("browser_click", {"ref": "@e4"}, task_id="t1", api_request_id="turn7:api:2")
        finally:
            plugin.click_scroller._run = orig_scroll
        check("plugin: the next response's click goes ahead", ok is None, str(ok))
        nav = json.dumps({"success": True, "url": "https://jobs.ashbyhq.com/acme/1", "snapshot": LONG_FORM})
        nav_out = json.loads(plugin._transform_tool_result("browser_navigate", {"url": "x"}, nav) or "{}")
        check("plugin: navigate results are unchanged (no extra snapshot, no note)",
              "jobo_page_after_action" not in nav_out and nav_out.get("snapshot", "").startswith(plugin.snapshot_filter.MARKER))
    finally:
        aa._snapshot, aa._ready_state = saved
        aa.reset_state()


VERDICT = "STATUS: submitted\nDETAIL: Application submitted.\nFIELDS_FILLED_JSON: {}\n"


def _drive(rounds: list[str]):
    calls, sets = [], []

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append(prompt)
        i = len(calls) - 1
        return (rounds[i] if i < len(rounds) else "session_id: s1\n" + VERDICT), False

    def resolve_field(line, *, cdp_url, job_id, failures):
        sets.append(line.strip())
        return f"FIELD_SET: {line.split(':', 1)[1].strip()}"

    saved = (agent_driver._run_hermes, agent_driver._resolve_field, agent_driver._dump_debug_transcript,
             agent_driver.session_api_calls)
    agent_driver._run_hermes = run_hermes
    agent_driver._resolve_field = resolve_field
    agent_driver._dump_debug_transcript = lambda *a, **k: None
    agent_driver.session_api_calls = lambda *a, **k: 1
    try:
        out = agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url="http://127.0.0.1:1",
                                           job_id=1535, resume_path="/tmp/r.pdf")
    finally:
        (agent_driver._run_hermes, agent_driver._resolve_field, agent_driver._dump_debug_transcript,
         agent_driver.session_api_calls) = saved
    return out, calls, sets


def test_field_batch():
    # Osprey 1535 on 2026-10-14 set these one per resume: 8 hermes restarts.
    turn = ("session_id: s1\nSetting the screening answers.\n"
            "NEED_FIELD: #4 = Yes\nNEED_FIELD: #5 = No\nNEED_FIELD: #6 = 0-1 year\nNEED_FIELD: #4 = Yes\n")
    out, calls, sets = _drive([turn, "session_id: s1\n" + VERDICT])
    check("three NEED_FIELD lines in one turn: all set, in order, duplicate dropped",
          sets == ["NEED_FIELD: #4 = Yes", "NEED_FIELD: #5 = No", "NEED_FIELD: #6 = 0-1 year"], str(sets))
    check("one resume answers all of them (2 hermes runs, not 4)",
          len(calls) == 2 and calls[1].count("FIELD_SET:") == 3 and out.status == "submitted", str(calls[1:])[:200])
    out, calls, sets = _drive(["session_id: s1\nNEED_FIELD: #2 = LIIT Springfield\n", "session_id: s1\n" + VERDICT])
    check("a single NEED_FIELD still works as before", sets == ["NEED_FIELD: #2 = LIIT Springfield"] and len(calls) == 2)
    many = "session_id: s1\n" + "".join(f"NEED_FIELD: #{i} = v{i}\n" for i in range(1, agent_driver.MAX_FIELD_ROUNDS + 4))
    out, calls, sets = _drive([many, "session_id: s1\n" + VERDICT])
    check(f"the {agent_driver.MAX_FIELD_ROUNDS}-set cap still holds inside one batch",
          len(sets) == agent_driver.MAX_FIELD_ROUNDS and "request limit reached" in calls[1], str(len(sets)))
    out, calls, sets = _drive(["session_id: s1\nNEED_FIELD: #1 = a\nSTATUS: stuck\nDETAIL: x\nFIELDS_FILLED_JSON: {}\n"])
    check("a STATUS line still beats NEED_FIELD lines", not sets and out.status == "stuck")
    check("prompt tells the agent to batch NEED_FIELD lines and that clicks return the page",
          "in the SAME turn, one `NEED_FIELD:` line each" in prompts.SYSTEM_PROMPT
          and "browser_press return the\n   same form view of the page AFTER the action" in prompts.SYSTEM_PROMPT
          and "browser_click/browser_type return no snapshot" not in prompts.SYSTEM_PROMPT)


def test_pacing():
    runner._next_start_by_host.clear()
    saved = (runner.constants.apply_delay_seconds, runner.constants.delay_jitter_seconds)
    runner.constants.apply_delay_seconds = lambda: 30
    runner.constants.delay_jitter_seconds = lambda: 0
    try:
        runner._note_submission("https://www.linkedin.com/jobs/view/1/", now=100.0)
        check("same host right after a submission: waits out the rest of the delay",
              runner._pacing_wait("https://www.linkedin.com/jobs/view/2/", now=110.0) == 20.0)
        check("same host after the delay: no wait",
              runner._pacing_wait("https://www.linkedin.com/jobs/view/2/", now=131.0) == 0.0)
        check("a different employer's ATS: no wait",
              runner._pacing_wait("https://boards.greenhouse.io/acme/jobs/9", now=101.0) == 0.0)
        check("host match ignores case and path", runner._pacing_wait("https://WWW.LinkedIn.com/x", now=101.0) == 29.0)
        runner._next_start_by_host.clear()
        runner.constants.apply_delay_seconds = lambda: 0
        runner._note_submission("https://www.linkedin.com/jobs/view/1/", now=100.0)
        check("JOBO_APPLY_DELAY_SEC=0 (tests, parallel test harness): never waits",
              runner._pacing_wait("https://www.linkedin.com/jobs/view/2/", now=100.0) == 0.0)
    finally:
        runner.constants.apply_delay_seconds, runner.constants.delay_jitter_seconds = saved
        runner._next_start_by_host.clear()


def main() -> int:
    test_after_action()
    test_plugin_wiring()
    test_field_batch()
    test_pacing()
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
