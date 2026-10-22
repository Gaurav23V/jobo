#!/usr/bin/env python3
"""Workday Save and Continue stayed disabled after the fields were filled (GHO-72).

Reputation 1464 (My Information) and Verizon 2219 (Voluntary Disclosures),
run_20261021_2350: one Save and Continue with a required field empty, Workday
showed "Errors Found" and disabled the button; the field was then filled and
the banner cleared, but the button never came back, and both agents spent
their whole budget re-selecting committed values. Workday re-enables the button
from an animation frame and the headed apply browser draws none on a locked
screen (module4/frame_keeper.py).
Covers, against scripts/workday_stuck_save_probe.html served as a
*.myworkdayjobs.com page in an own headless Chromium (never 9333):
  [1] NEED_CLICK Save and Continue with required fields unanswered is refused
      before any click, naming them
  [2] a disabled button names only what is really unanswered (not the
      answered radio group, phone code chip or Race/Ethnicity grid) plus
      Workday's own error list
  [3] NEED_FIELDS: one checked box answers the Race/Ethnicity grid, and the
      site's own error list is printed
  [4] a search prompt whose lone result Workday picks itself is reported set
  [5] disabled with nothing unanswered and nothing flagged: one re-check,
      then report, never re-select; a page drawing no frames is named
  [6] the Hermes plugin veto: blocks browser_click on the footer button while
      a field is unanswered, lets it through after two blocks or once filled
  [7] a value picked from the options our own failure offered is not
      abandoned by the 3-strike rule
  [8] frame keeper: every page target, also one opened later, is held with
      focus emulation
"""
import functools
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("JOBO_PERSONAL_DIR", str(REPO / "personal.example"))

from module4 import agent_driver, clicker, form_filler as f, frame_keeper  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HOST = "wd.contoso.myworkdayjobs.com"
_spec = importlib.util.spec_from_file_location(
    "wd_guard_test", REPO / "hermes_plugin" / "jobo-browser" / "workday_guard.py")
wd_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd_guard)

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)


def serve() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(REPO / "scripts"))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def browser_checks(port: int) -> None:
    url = f"http://{HOST}:{port}/workday_stuck_save_probe.html"
    with tempfile.TemporaryDirectory(prefix="jobo-wd-stuck-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + profile,
             f"--host-resolver-rules=MAP {HOST} 127.0.0.1",
             # Workday is https; the setter needs a secure context (crypto.randomUUID).
             f"--unsafely-treat-insecure-origin-as-secure=http://{HOST}:{port}", "about:blank"],
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

                def load():
                    page.goto(url)
                    page.wait_for_timeout(200)

                def failed_save():
                    # The agent's own browser_click, on a page drawing no frames.
                    page.evaluate("window.__noFrames = true")
                    page.click("[data-automation-id=pageFooterNextButton]")
                    page.wait_for_timeout(700)

                # [1]
                load()
                res = clicker.click(cdp, "Save and Continue")
                msg = res.as_agent_message()
                check("[1] footer click refused while fields are unanswered",
                      res.guard_refusal and msg.startswith("BLOCKED by jobo") and not res.delivered, msg[:200])
                check("[1] no click reached the page", page.evaluate("window.__saveClicks") == 0)
                check("[1] names every unanswered field",
                      all(n in msg for n in ("How Did You Hear", "employed by Contoso", "Given Name", "Race/Ethnicity")),
                      msg[:400])

                # [2]
                page.fill("#name--legalName--firstName", "Pat")
                page.check("#prev-no")
                page.check("#race-a")
                failed_save()
                check("[2] probe: button stays disabled without frames",
                      page.evaluate("document.querySelector('[data-automation-id=pageFooterNextButton]').disabled"))
                res = clicker.click(cdp, "Save and Continue")
                msg = res.as_agent_message()
                check("[2] disabled click names only the empty prompt",
                      res.disabled and res.missing == ["How Did You Hear About Us? (nothing selected)"], str(res.missing))
                check("[2] no false 'empty' for radio group, phone code or race grid",
                      not any(n in res.note for n in ("employed by", "Phone Code", "Group A", "Group B")), res.note[:300])
                check("[2] Workday's own error list is quoted",
                      "The site's own error list: How Did You Hear About Us?" in msg, msg[:400])

                # [3]
                ls = f.list_fields(cdp)
                f.remember_listing(cdp, ls)
                summary = ls.required_summary()
                empty = [x.label for x in ls.fields if x.missing_required()]
                check("[3] race grid answered by one box", not any("Group" in e or "Prefer" in e for e in empty), str(empty))
                check("[3] only the prompt is required-empty",
                      len(empty) == 1 and empty[0].startswith("How Did You Hear"), str(empty))
                check("[3] site's own flags printed",
                      "The site itself still flags: How Did You Hear About Us?" in summary, summary[-300:])

                # [4]
                src = next(x for x in ls.fields if x.label.startswith("How Did You Hear"))
                res4 = f.set_field(cdp, f"#{src.index}", "LinkedIn", index=src.index)
                check("[4] lone search result picked by Workday is reported set",
                      res4.ok and res4.readback == "LinkedIn corporate page", f"{res4.ok} {res4.readback!r} {res4.note}")
                check("[4] the reply says Workday picked it", "Workday picked" in res4.as_agent_message())

                # [5] (a browser on a locked screen runs no animation frame at all)
                page.wait_for_timeout(300)
                page.evaluate("window.requestAnimationFrame = () => 0")
                res = clicker.click(cdp, "Save and Continue")
                msg = res.as_agent_message()
                check("[5] nothing unanswered, nothing flagged -> stuck, not refill",
                      res.stuck and not res.missing and "re-selecting values will not help" in msg, msg[:400])
                check("[5] a page drawing no frames is named", "drawing no frames" in msg, msg[:400])
                check("[5] nothing was saved", not page.evaluate("window.__saved"))

                # [6]
                load()
                env = {"BROWSER_CDP_URL": cdp, "JOBO_UPLOAD_RESUME_NAME": "X_Resume_1_abcdef.pdf"}
                name = lambda ref: {"e9": "button Save and Continue", "e3": "button Back"}.get(ref, "")
                wd_guard._blocks.clear()
                with patch.dict(os.environ, env):
                    first = wd_guard.check_tool_call("browser_click", {"ref": "@e9"}, name)
                    other = wd_guard.check_tool_call("browser_click", {"ref": "@e3"}, name)
                    second = wd_guard.check_tool_call("browser_click", {"ref": "@e9"}, name)
                    third = wd_guard.check_tool_call("browser_click", {"ref": "@e9"}, name)
                    page.fill("#name--legalName--firstName", "Pat")
                    page.check("#prev-no")
                    page.check("#race-a")
                    page.evaluate("""() => { const p = document.querySelector('#source--source')
                      .closest('[data-automation-id=multiselectInputContainer]').querySelector('ul');
                      p.innerHTML = '<li><div data-automation-id="selectedItem" title="Corporate Website">Corporate Website</div></li>'; }""")
                    filled = wd_guard.check_tool_call("browser_click", {"ref": "@e9"}, name)
                with patch.dict(os.environ, {"BROWSER_CDP_URL": cdp, "JOBO_UPLOAD_RESUME_NAME": ""}):
                    off = wd_guard.check_tool_call("browser_click", {"ref": "@e9"}, name)
                check("[6] plugin blocks the footer click while unanswered",
                      bool(first) and first.startswith("BLOCKED by jobo") and bool(second), str(first)[:200])
                check("[6] other buttons pass", other is None)
                check("[6] third try passes (a misread cannot wall the form)", third is None)
                check("[6] passes once every field is answered", filled is None, str(filled)[:200])
                check("[6] off outside a jobo apply run", off is None)

                # [8]
                keeper = frame_keeper.FrameKeeper(cdp)
                keeper.start()
                try:
                    first_id = next(t["id"] for t in json.load(urllib.request.urlopen(cdp + "/json/list"))
                                    if t["type"] == "page")
                    for _ in range(60):
                        if first_id in keeper.emulated:
                            break
                        time.sleep(0.05)
                    check("[8] existing page held with focus emulation", first_id in keeper.emulated)
                    later = page.context.new_page()
                    later.goto("about:blank")
                    later_id = next(t["id"] for t in json.load(urllib.request.urlopen(cdp + "/json/list"))
                                    if t["type"] == "page" and t["id"] != first_id)
                    for _ in range(60):
                        if later_id in keeper.emulated:
                            break
                        time.sleep(0.05)
                    check("[8] a tab opened later is held too", later_id in keeper.emulated)
                    later.close()
                    for _ in range(60):
                        if later_id not in keeper.emulated:
                            break
                        time.sleep(0.05)
                    check("[8] a closed tab is dropped", later_id not in keeper.emulated)
                finally:
                    keeper.stop()
                check("[8] keeper stops", not keeper.alive())
                with patch.dict(os.environ, {"JOBO_FRAME_KEEPER": "0"}):
                    check("[8] JOBO_FRAME_KEEPER=0 turns it off", frame_keeper.ensure(cdp) is None)
                # Parallel workers can share a process: each stops only its own keeper,
                # and stopping one that never started must not raise.
                try:
                    frame_keeper.FrameKeeper(cdp).stop()
                    unstarted_ok = True
                except Exception as e:
                    unstarted_ok, err = False, e
                check("[8] stopping a keeper that never started is fine", unstarted_ok,
                      "" if unstarted_ok else repr(err))
                with patch.dict(os.environ, {"JOBO_FRAME_KEEPER": "1"}):
                    mine = frame_keeper.ensure(cdp)
                    # Another worker's keeper, registered but not started yet (the
                    # race test_jobfound_stages hit). No socket, so no network guard.
                    other_url = "http://127.0.0.1:9"
                    frame_keeper._KEEPERS[other_url] = frame_keeper.FrameKeeper(other_url)
                    frame_keeper.stop(other_url)
                    check("[8] another worker's stop leaves this keeper running",
                          mine is not None and mine.alive() and other_url not in frame_keeper._KEEPERS)
                    check("[8] ensure is idempotent per browser", frame_keeper.ensure(cdp + "/") is mine)
                    frame_keeper.stop(cdp)
                    check("[8] its own stop ends it", not mine.alive())
        finally:
            proc.kill()
            proc.wait(timeout=10)


def strike_checks() -> None:
    """[7] offline: the driver's per-field strike rule."""
    replies = iter([
        f.SetFieldResult(False, note="no matching option", no_match=True, available_options=["Campus Campaign", "Career Websites"]),
        f.SetFieldResult(False, note="no matching option", no_match=True, available_options=["Campus Campaign", "Career Websites"]),
        f.SetFieldResult(False, note='"Career Websites" is a group', no_match=True, available_options=["Corporate Website"]),
        f.SetFieldResult(True, matched="How Did You Hear About Us?", readback="Corporate Website"),
    ])
    calls = []

    def fake_set(cdp_url, hint, value, index=None):
        calls.append(value)
        return next(replies)

    failures: dict[str, int] = {}
    agent_driver._JOB = agent_driver._JobContext(7)
    with patch.object(f, "set_field", fake_set), patch.object(f, "listed_field", lambda *a, **k: None):
        for value in ("LinkedIn", "Job Board", "Career Websites", "Corporate Website"):
            out = agent_driver._resolve_field(f"NEED_FIELD: #2 = {value}", cdp_url="http://x", job_id=7, failures=failures)
    check("[7] the offered option is tried after three failures", calls[-1:] == ["Corporate Website"], str(calls))
    check("[7] and reported set, not abandoned", out.startswith("FIELD_SET:"), out[:120])
    # A guess that was never offered is still abandoned.
    failures = {"#2": 3}
    agent_driver._JOB = agent_driver._JobContext(7)
    agent_driver._JOB.offered_options["#2"] = ["Corporate Website"]
    with patch.object(f, "set_field", fake_set), patch.object(f, "listed_field", lambda *a, **k: None):
        out = agent_driver._resolve_field("NEED_FIELD: #2 = Indeed", cdp_url="http://x", job_id=7, failures=failures)
    check("[7] a value never offered is still abandoned", out.startswith("FIELD_SET_ABANDONED"), out[:120])
    agent_driver._JOB = agent_driver._JobContext()


def main() -> int:
    browser_checks(serve())
    strike_checks()
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
