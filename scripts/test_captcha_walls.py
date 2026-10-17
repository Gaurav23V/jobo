#!/usr/bin/env python3
"""Captcha walls from the 2026-10-11 run (TODO item 15).

  * Redpine 1485: in-form canvas jigsaw "Slide to verify". The agent spent 8.6
    min and failed. module4/slider_captcha.py solves it in code (NEED_SLIDER).
  * Kestrelway 1477 / 1505: DataDome bot wall on click.appcast.io, a click
    tracker. It cannot be solved (slider and audio both rejected, measured);
    hermes_plugin/jobo-browser/captcha_guard.py stops the agent fast and
    sends it once more through the tracker / to the employer's own page.

Part 1 runs the solver against scripts/slider_captcha_probe.html (Redpine's
SliderCaptcha class, copied verbatim) in its own headless Chromium on a random
port, never 9333. Parts 2-4 are offline: the guard on the real snapshot text
of 1477 and 1485, the plugin's filter ordering, and the driver's NEED_SLIDER
loop with _run_hermes stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver, prompts, slider_captcha  # noqa: E402

PROBE = Path(__file__).with_name("slider_captcha_probe.html")
PLUGIN_DIR = ROOT / "hermes_plugin" / "jobo-browser"

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Part 1: slider solver on the replica ------------------------------------

def test_slider(cdp: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        page = p.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
        state = lambda: page.evaluate("() => window.__state()")  # noqa: E731

        page.goto("about:blank")
        none = slider_captcha.solve(cdp)
        check("no slider on the page: fails fast with a clear note",
              not none.ok and "no supported slide-to-verify" in none.note, none.note)

        targets = [8, 37, 90, 141, 199, 250, 275]  # left edge to right edge
        solved, times = 0, []
        for t in targets:
            page.goto(PROBE.as_uri() + f"?target={t}")
            page.wait_for_function("() => window.__cap && window.__cap.targetX === %d" % t, timeout=5000)
            t0 = time.monotonic()
            res = slider_captcha.solve(cdp)
            times.append(time.monotonic() - t0)
            s = state()
            ok = res.ok and s["solved"] and s["token"].startswith("ok-")
            solved += ok
            if not ok:
                print(f"   target {t}: {res.note} state={s}")
        check(f"jigsaw solved for every gap position ({solved}/{len(targets)})", solved == len(targets))
        check("each solve takes under 15 s", max(times) < 15, f"{max(times):.1f}s")
        check("solver never submits the form", state()["submits"] == 0)

        page.goto(PROBE.as_uri())
        page.wait_for_function("() => window.__cap && window.__cap.targetX > 0", timeout=5000)
        first = slider_captcha.solve(cdp)
        again = slider_captcha.solve(cdp)
        check("random gap (site's own seed) solved on the first drag",
              first.ok and first.attempts == 1, first.note)
        check("asking again after success: 'already verified', no new drag",
              again.ok and "already verified" in again.note and state()["fails"] == 0, again.note)
        msg = first.as_agent_message()
        check("agent message says SLIDER_SOLVED and not to touch it again",
              msg.startswith("SLIDER_SOLVED") and "Do not touch" in msg)


# --- Part 2: guard on the real snapshots --------------------------------------

# Kestrelway 1477, 2026-10-11 (state.db message 44023), trimmed.
KESTRELWAY_WALL = {
    "success": True,
    "snapshot": (
        "- Iframe \"DataDome CAPTCHA\" [ref=e1]\n  - generic\n    - paragraph\n"
        "      - StaticText \"Verification Required\"\n"
        "          - button \"Switch to the audio verification\" [ref=e5]\n"
        "        - button \"Reload the verification\" [ref=e3]\n"
    ),
    "frame_tree": {"top": {"url": (
        "https://geo.captcha-delivery.com/captcha/?initialCid=AHrl&hash=8C14&cid=Gdq&t=fe"
        "&referer=https%3A%2F%2Fclick.appcast.io%2Ft%2FAbCdEfGh0123456789xyzQW&s=57500")}},
}
# Kestrelway 1505 (message 39033): the wall's own page, title says blocked.
KESTRELWAY_WALL_PAGE = {
    "success": True,
    "url": "https://geo.captcha-delivery.com/captcha/?initialCid=x&referer=https%3A%2F%2Fclick.appcast.io%2Ft%2FZZZZ9",
    "title": "You have been blocked | Appcast Verification",
    "snapshot": "- paragraph\n  - StaticText \"Slide right to secure your access\"\n",
}
# Redpine 1485 apply modal (2026-10-11), trimmed.
DUNLIN_FORM = {
    "success": True,
    "snapshot": (
        "- textbox \"Full name\" [required, ref=e30]\n"
        "- LabelText\n  - StaticText \"Verify you're human\"\n"
        "- generic\n  - generic [ref=e59] clickable [cursor:pointer]\n  - Canvas\n  - Canvas\n  - Canvas\n"
        "- generic\n  - image\n  - StaticText \"Slide to verify\"\n"
        "- button \"Submit application\" [ref=e37]\n"
    ),
}
KESTRELWAY_JOB_PAGE = {
    "success": True, "url": "https://careers.kestrelway.example/job/software-development-engineer-ii/x/R-000000/",
    "snapshot": "- link \"Apply Now\" [ref=e20]\n",
}
TRACKER = "https://click.appcast.io/t/ZZZZ9_exampleTrackerToken0000000000000000000000000000000000000000"


def test_guard():
    g = _load("jobo_captcha_guard_test", PLUGIN_DIR / "captcha_guard.py")
    dump = json.dumps
    check("classify: Kestrelway 1477 iframe wall is datadome", g.classify(KESTRELWAY_WALL) == "datadome")
    check("classify: Kestrelway 1505 wall page is datadome", g.classify(KESTRELWAY_WALL_PAGE) == "datadome")
    check("classify: Redpine form is jigsaw", g.classify(DUNLIN_FORM) == "jigsaw")
    check("classify: an ordinary job page is nothing", g.classify(KESTRELWAY_JOB_PAGE) is None)
    check("wall_tracker: tracker URL recovered from the captcha iframe's referer",
          g.wall_tracker(KESTRELWAY_WALL).startswith("https://click.appcast.io/t/AbCdEfGh"))

    # The 1505 sequence: tracker, (redirect to Kestrelway), wall, ...
    g.reset_state()
    check("1st tracker visit allowed", g.check_tool_call("browser_navigate", {"url": TRACKER}) is None)
    first = json.loads(g.annotate("browser_navigate", dump(KESTRELWAY_WALL)))
    snap = first["snapshot"]
    check("first wall: notice on top, says one more visit to the tracker URL",
          snap.startswith("[jobo: this page is a DataDome") and "AbCdEfGh" in snap and "ONE thing" in snap, snap[:300])
    check("while walled: browser_vision blocked",
          (g.check_tool_call("browser_vision", {"question": "where is the slider"}) or "").startswith("BLOCKED"))
    check("while walled: synthetic mouse drag blocked",
          (g.check_tool_call("browser_cdp", {"method": "Input.dispatchMouseEvent", "params": {}}) or "").startswith("BLOCKED"))
    check("navigating to captcha-delivery.com itself is blocked",
          (g.check_tool_call("browser_navigate", {"url": KESTRELWAY_WALL_PAGE["url"]}) or "").startswith("BLOCKED"))
    check("2nd tracker visit (the one the notice asks for) allowed",
          g.check_tool_call("browser_navigate", {"url": TRACKER}) is None)
    check("clean page clears the wall flag (no notice)", g.annotate("browser_snapshot", dump(KESTRELWAY_JOB_PAGE)) is None)
    check("after a clean page: browser_vision allowed again", g.check_tool_call("browser_vision", {"question": "x"}) is None)
    third = g.check_tool_call("browser_cdp", {"method": "Page.navigate", "params": {"url": TRACKER}}) or ""
    check("3rd tracker visit (any route) blocked, points at the employer's Apply link",
          third.startswith("BLOCKED") and "Apply" in third, third[:200])
    second = json.loads(g.annotate("browser_snapshot", dump(KESTRELWAY_WALL)))["snapshot"]
    check("second wall: notice says end NOW with captcha_failed",
          "STATUS: captcha_failed" in second and "ONE thing" not in second, second[:400])

    g.reset_state()
    direct = json.loads(g.annotate("browser_snapshot", dump(KESTRELWAY_WALL_PAGE | {"url": "https://www.example-ats.com/apply"})))
    check("wall not from a tracker: straight to captcha_failed",
          "STATUS: captcha_failed" in direct["snapshot"] and "ONE thing" not in direct["snapshot"])
    jig = json.loads(g.annotate("browser_snapshot", dump(DUNLIN_FORM)))["snapshot"]
    check("jigsaw notice tells the agent to print NEED_SLIDER", "NEED_SLIDER" in jig and "Do not drag" in jig)
    check("non-navigation tools are never blocked on a clean page",
          g.check_tool_call("browser_click", {"ref": "e1"}) is None
          and g.check_tool_call("browser_cdp", {"method": "Runtime.evaluate", "params": {}}) is None)


def test_plugin_order():
    """A notice must survive the snapshot filter (it keeps only '- role' lines)."""
    plugin = _load("jobo_browser_plugin_test", PLUGIN_DIR / "__init__.py")
    plugin.captcha_guard.reset_state()
    plugin.account_guard.reset_state()
    padding = "".join(f"- paragraph\n  - StaticText \"Why is this step needed? reason {i} " + "x" * 60 + "\"\n"
                      for i in range(40))
    big = dict(KESTRELWAY_WALL_PAGE, snapshot=KESTRELWAY_WALL_PAGE["snapshot"] + padding)
    out = plugin._transform_tool_result("browser_snapshot", {}, json.dumps(big))
    snap = json.loads(out)["snapshot"] if out else ""
    check("long wall snapshot (filtered): DataDome notice still on top",
          snap.startswith("[jobo: this page is a DataDome") and len(json.dumps(big)) > 1500, snap[:200])
    plugin.captcha_guard.reset_state()
    blocked = plugin._pre_tool_call("browser_navigate", {"url": "https://geo.captcha-delivery.com/captcha/?x=1"})
    check("plugin pre_tool_call blocks a captcha-delivery navigate", (blocked or {}).get("action") == "block")
    ok = plugin._pre_tool_call("browser_navigate", {"url": "https://jobs.lever.co/acme/1"})
    check("plugin pre_tool_call lets an ordinary ATS through", ok is None)

    # Click scroller (Redpine 1485 retry: click on a button just below the fold hit <html>).
    cs = plugin.click_scroller

    def make_runner(fail_scroll=False):
        """Fake agent-browser recording (task, command, first arg)."""
        log = []

        def run(task, cmd, args):
            log.append((task, cmd, "eval-on" if cmd == "eval" and "setProperty('scroll-behavior', 'auto'" in args[0]
                        else "eval-off" if cmd == "eval" else args[0]))
            if cmd == "scrollintoview" and fail_scroll:
                raise RuntimeError("daemon gone")
            return {"success": True}
        return run, log

    run, log = make_runner()
    check("click scroller: smooth scroll off -> scrollintoview @e19 -> page style restored, same task",
          cs.before_click("browser_click", {"ref": "@e19"}, "t1", runner=run)
          and log == [("t1", "eval", "eval-on"), ("t1", "scrollintoview", "@e19"), ("t1", "eval", "eval-off")], str(log))
    run, log = make_runner()
    cs.before_click("browser_click", {"ref": "e7"}, "t1", runner=run)
    check("click scroller: bare ref normalised to @e7", ("t1", "scrollintoview", "@e7") in log)
    run, log = make_runner()
    for tool, a in (("browser_type", {"ref": "@e3", "text": "x"}), ("browser_click", {"ref": "button.apply"}),
                    ("browser_navigate", {"url": "https://x"})):
        cs.before_click(tool, a, "t1", runner=run)
    check("click scroller: only browser_click with a snapshot ref triggers a scroll", log == [])
    run, log = make_runner(fail_scroll=True)
    check("click scroller: a failing scroll fails open and still restores the page style",
          cs.before_click("browser_click", {"ref": "@e1"}, "t1", runner=run) is False
          and log[-1] == ("t1", "eval", "eval-off"), str(log))
    orig = cs._run
    run, log = make_runner()
    cs._run = run
    try:
        res = plugin._pre_tool_call("browser_click", {"ref": "@e21"}, task_id="t9")
    finally:
        cs._run = orig
    check("plugin pre_tool_call runs the scroll for a click and does not block it",
          res is None and ("t9", "scrollintoview", "@e21") in log)


# --- Part 3: driver loop -------------------------------------------------------

VERDICT = "STATUS: submitted\nDETAIL: Application submitted.\nFIELDS_FILLED_JSON: {}\n"


def _drive(rounds: list[str], slider_ok=True):
    calls, slider_calls = [], []

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append(prompt)
        i = len(calls) - 1
        return (rounds[i] if i < len(rounds) else "session_id: s1\n" + VERDICT), False

    def resolve_slider(*, cdp_url, job_id):
        slider_calls.append(cdp_url)
        return "SLIDER_SOLVED: verified on attempt 1" if slider_ok else "SLIDER_FAILED: not verified"

    saved = (agent_driver._run_hermes, agent_driver._resolve_slider, agent_driver._dump_debug_transcript,
             agent_driver.session_api_calls)
    agent_driver._run_hermes = run_hermes
    agent_driver._resolve_slider = resolve_slider
    agent_driver._dump_debug_transcript = lambda *a, **k: None
    agent_driver.session_api_calls = lambda *a, **k: 1
    try:
        out = agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url="http://127.0.0.1:1",
                                           job_id=1485, resume_path="/tmp/r.pdf")
    finally:
        (agent_driver._run_hermes, agent_driver._resolve_slider, agent_driver._dump_debug_transcript,
         agent_driver.session_api_calls) = saved
    return out, calls, slider_calls


def test_driver():
    out, calls, sl = _drive(["session_id: s1\nAll fields filled.\nNEED_SLIDER\n", "session_id: s1\n" + VERDICT])
    check("NEED_SLIDER: solver called once, result fed back, job submitted",
          len(sl) == 1 and calls[1].startswith("SLIDER_SOLVED") and out.status == "submitted", f"{sl} {out}")
    out, calls, sl = _drive(["session_id: s1\nNEED_SLIDER\n"] * 4, slider_ok=False)
    check(f"slider requests capped at {agent_driver.MAX_SLIDER_ROUNDS}",
          len(sl) == agent_driver.MAX_SLIDER_ROUNDS and "request limit reached" in calls[agent_driver.MAX_SLIDER_ROUNDS + 1],
          f"{len(sl)} solves")
    out, calls, sl = _drive(["session_id: s1\nNEED_SLIDER\nSTATUS: captcha_failed\nDETAIL: x\nFIELDS_FILLED_JSON: {}\n"])
    check("a STATUS line beats a stray NEED_SLIDER", not sl and out.status == "captcha_failed")
    check("system prompt teaches NEED_SLIDER and the DataDome rule",
          "NEED_SLIDER" in prompts.SYSTEM_PROMPT and "captcha-delivery.com" in prompts.SYSTEM_PROMPT)


def test_circuit_breaker(tmp: Path):
    """2026-10-12: two unrelated jobs tripped the breaker for 'linkedin.com' (every job_url is a
    LinkedIn listing), which drops every queued job. Listing hosts must never be counted."""
    from module4 import circuit_breaker as cb
    saved = cb.STATE_PATH
    cb.STATE_PATH = tmp / "cb.json"
    try:
        for jid in (1394, 1485):
            cb.record_failure(f"https://www.linkedin.com/jobs/view/{jid}/", jid, "apply flow is broken")
        check("breaker: linkedin.com listing URLs never trip", cb.is_tripped("https://www.linkedin.com/jobs/view/9/") is None
              and not cb.STATE_PATH.exists())
        for jid in (1, 2):
            cb.record_failure("https://dead-ats.example.com/apply", jid, "403 Forbidden on every request")
        check("breaker: a real ATS host still trips after 2 jobs", cb.is_tripped("https://dead-ats.example.com/x") is not None)
    finally:
        cb.STATE_PATH = saved


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-cb-") as d:
        test_circuit_breaker(Path(d))
    test_guard()
    test_plugin_order()
    test_driver()
    with tempfile.TemporaryDirectory(prefix="jobo-captcha-walls-", ignore_cleanup_errors=True) as tmpd:
        profile = Path(tmpd) / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + str(profile),
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = profile / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            test_slider(cdp)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
