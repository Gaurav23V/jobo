#!/usr/bin/env python3
"""NEED_UPLOAD on three live shapes that main's uploader got wrong (measured 2026-10-04).

1. Eastlake 1266, Greenhouse (job-boards.greenhouse.io): on change the app uploads
   the file and then REMOVES <input id=resume>, showing its own "resume.pdf
   [Remove file]" chip. Main read the vanished input as "original logical upload
   slot missing" and told the agent UPLOAD_FAILED while the site held the file.
   The agent, told it failed, kept re-asking and re-opening the form URL
   (wiping it), until the 4-request cap ran out. A repeat request after a good
   upload also failed ("no <input type=file> found"), burning another round.
2. Eastlake 1266 on www.eastlake.com/careers/jobs/<id>: the Greenhouse form is a
   cross-origin iframe, i.e. its own CDP target. Main only scanned page targets,
   so every request answered "no <input type=file> found in any open tab".
3. Litware 1316, SAP SuccessFactors "My Documents" tiles: the "+" control calls
   .click() on an <input type=file> that is never attached to the document, so
   no DOM walk can find it and a click opens the native file chooser. Main
   answered "no <input type=file> found". Patched code clicks the tile's own
   upload control once with Page.setInterceptFileChooserDialog on and answers
   the chooser by backendNodeId.

Safety checks: a page whose only control is "Submit application" gets no click;
a site-side rejection is still UPLOAD_FAILED.

Page: scripts/upload_chooser_probe.html (local replica of those DOM shapes),
served over two local origins so the iframe really is out-of-process.
Self-isolated: own headless Chromium on a random port (never 9333).
Run: bash scripts/run_regression_tests.sh test_upload_chooser_iframe
"""
from __future__ import annotations

import functools
import http.server
import json
import socketserver
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import uploader  # noqa: E402
from scripts.test_upload_stability import Stability  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
CDP = ""
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:62s} {note[:220]}", flush=True)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/wrapper.html"):
            port = self.server.server_address[1]
            body = (f'<!doctype html><html><body><h1>Careers at Example</h1>'
                    f'<iframe id="gh" title="Greenhouse Job Board" width="900" height="700" '
                    f'src="http://localhost:{port}/upload_chooser_probe.html?shape=gh"></iframe>'
                    f'</body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _http(path: str, method: str = "GET"):
    with urllib.request.urlopen(urllib.request.Request(CDP + path, method=method), timeout=5) as r:
        return json.load(r)


def _open_only(url: str) -> dict:
    """Open url in a fresh tab and close every other probe tab (keep the guard)."""
    tab = _http(f"/json/new?{url}", "PUT")
    for t in _http("/json/list"):
        if t.get("type") == "page" and t["id"] not in (tab["id"], Stability.guard["id"]):
            try:
                _http(f"/json/close/{t['id']}")
            except Exception:
                pass
    time.sleep(1.2)
    return tab


def _state(ws_url: str) -> dict:
    with uploader._Cdp(ws_url) as c:
        r = c.call("Runtime.evaluate", {"expression": "window.__state()", "returnByValue": True})
        return json.loads(r["result"]["result"]["value"])


def _frame_ws() -> str:
    for _ in range(40):
        for t in _http("/json/list"):
            if t.get("type") == "iframe" and "upload_chooser_probe" in t.get("url", ""):
                return t["webSocketDebuggerUrl"]
        time.sleep(0.25)
    return ""


def _checks(base: str, resume: Path, cover: Path) -> int:
    print("\n  -- SuccessFactors tiles: no DOM input, native chooser --")
    tab = _open_only(f"{base}/upload_chooser_probe.html?shape=sf")
    r = uploader.upload_file(CDP, str(resume), want="resume")
    st = _state(tab["webSocketDebuggerUrl"])
    check("SF resume tile: attached via its own + control", r.ok and st["picked"].get("51:") == resume.name,
          f"{r.ok} {r.detail[:120]} | {st}")
    check("SF: only the resume tile was clicked, never Submit",
          st["clicks"] == {"51:": 1} and st["applied"] == 0, json.dumps(st["clicks"]))
    r = uploader.upload_file(CDP, str(cover), want="cover")
    st = _state(tab["webSocketDebuggerUrl"])
    check("SF cover tile: cover goes to the cover tile", r.ok and st["picked"].get("53:") == cover.name
          and st["picked"].get("51:") == resume.name, f"{r.ok} {r.detail[:100]} | {st['picked']}")

    tab = _open_only(f"{base}/upload_chooser_probe.html?shape=apply")
    r = uploader.upload_file(CDP, str(resume), want="resume")
    st = _state(tab["webSocketDebuggerUrl"])
    check("page with only Submit: nothing clicked, honest failure", (not r.ok) and st["applied"] == 0,
          f"{r.ok} {r.detail[:100]}")

    print("\n  -- Greenhouse: the app replaces its input with a file chip --")
    tab = _open_only(f"{base}/upload_chooser_probe.html?shape=gh")
    r = uploader.upload_file(CDP, str(resume), want="resume")
    st = _state(tab["webSocketDebuggerUrl"])
    check("GH resume: UPLOAD_DONE when the site shows its chip", r.ok and st["picked"].get("resume") == resume.name,
          f"{r.ok} {r.detail[:140]}")
    r = uploader.upload_file(CDP, str(cover), want="cover")
    st = _state(tab["webSocketDebuggerUrl"])
    check("GH cover after resume: UPLOAD_DONE", r.ok and st["picked"].get("cover_letter") == cover.name,
          f"{r.ok} {r.detail[:140]}")
    writes = st["writes"]
    r = uploader.upload_file(CDP, str(resume), want="resume")
    st = _state(tab["webSocketDebuggerUrl"])
    check("GH repeat request: 'already attached', nothing re-uploaded", r.ok and st["writes"] == writes,
          f"{r.ok} {r.detail[:140]}")

    tab = _open_only(f"{base}/upload_chooser_probe.html?shape=gh-reject")
    r = uploader.upload_file(CDP, str(resume), want="resume")
    st = _state(tab["webSocketDebuggerUrl"])
    check("GH site rejects the file: still UPLOAD_FAILED", (not r.ok) and "Unable to upload" in st["text"],
          f"{r.ok} {r.detail[:140]}")

    print("\n  -- Greenhouse embedded cross-origin (out-of-process iframe) --")
    _open_only(f"{base}/wrapper.html")
    fws = _frame_ws()
    check("fixture: the embedded form is its own CDP target (OOPIF)", bool(fws), "")
    if fws:
        r = uploader.upload_file(CDP, str(resume), want="resume")
        st = _state(fws)
        check("OOPIF resume: found in the iframe target and attached",
              r.ok and st["picked"].get("resume") == resume.name, f"{r.ok} {r.detail[:140]}")
    return sum(1 for _, ok, _ in results if not ok)


def _driver_checks() -> None:
    """Offline: 'already attached' answers must not use up MAX_UPLOAD_ROUNDS."""
    from module4 import agent_driver

    print("\n  -- driver: repeat requests for an attached file are not charged --")

    def run(rounds: list[str], answers: list[str]) -> tuple[list[str], list[str]]:
        asked: list[str] = []
        prompts: list[str] = []

        def fake_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
            prompts.append(prompt)
            i = len(prompts) - 1
            out = rounds[i] if i < len(rounds) else "STATUS: stuck\nDETAIL: x\nFIELDS_FILLED_JSON: {}\n"
            return ("session_id: s_up\n" if i == 0 else "") + out, False

        def fake_upload(line, *, cdp_url, resume_path, cover_path, job_id):
            asked.append(line)
            return answers[min(len(asked), len(answers)) - 1]

        saved = (agent_driver._run_hermes, agent_driver._resolve_upload, agent_driver._dump_debug_transcript)
        agent_driver._run_hermes, agent_driver._resolve_upload = fake_hermes, fake_upload
        agent_driver._dump_debug_transcript = lambda *a, **k: None
        try:
            agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url="http://127.0.0.1:1",
                                         job_id=998, is_retry=False, resume_path="/tmp/r.pdf",
                                         cover_path="/tmp/c.pdf", budget_boost=False)
        finally:
            agent_driver._run_hermes, agent_driver._resolve_upload, agent_driver._dump_debug_transcript = saved
        return asked, prompts

    done = "UPLOAD_DONE: resume.pdf attached to id=\"resume\""
    already = "UPLOAD_DONE: ALREADY_ATTACHED resume.pdf: the site already shows it in 'Resume/CV resume.pdf'."
    # Job 1266's shape: one real upload, three "is it still there?" repeats, then the cover.
    asked, prompts = run(["NEED_UPLOAD: resume\n"] * 4 + ["NEED_UPLOAD: cover\n"], [done, already, already, already, done])
    check("cover still allowed after 1 upload + 3 already-attached repeats",
          len(asked) == 5 and "cover" in asked[-1] and not any("request limit reached" in p for p in prompts),
          f"performed={len(asked)}")
    # A looping agent is still cut off.
    asked, prompts = run(["NEED_UPLOAD: resume\n"] * 12, [done] + [already] * 20)
    cap = agent_driver.MAX_UPLOAD_ROUNDS + getattr(agent_driver, "MAX_ALREADY_REFUNDS", 0)
    check("a looping agent is still refused eventually",
          len(asked) <= cap and any("request limit reached" in p for p in prompts), f"performed={len(asked)} cap={cap}")


def main() -> int:
    global CDP
    results.clear()
    _driver_checks()
    Stability.setUpClass()
    server = _Server(("127.0.0.1", 0), functools.partial(_Handler, directory=str(SCRIPTS)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        CDP = Stability.base
        assert not CDP.endswith(":9333")
        tmp = Path(Stability.tmp.name)
        resume = tmp / "resume.pdf"
        resume.write_bytes(Stability.pdf.read_bytes())
        cover = tmp / "cover.pdf"
        cover.write_bytes(b"%PDF-1.4\n% cover fixture\n%%EOF\n")
        fails = _checks(f"http://127.0.0.1:{server.server_address[1]}", resume, cover)
    finally:
        server.shutdown()
        Stability.tearDownClass()
    passed = len(results) - fails
    print(f"\n{passed}/{len(results)} passed" + ("" if not fails else f"; {fails} FAILED"))
    return 1 if fails or not results else 0


if __name__ == "__main__":
    sys.exit(main())
