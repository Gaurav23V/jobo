#!/usr/bin/env python3
"""A resume upload behind Paylocity's "Apply with resume" modal (GHO-116).

Run 2026-10-31 22:00 IST, job 2521 on recruiting.paylocity.com: the apply
page opens a Bootstrap modal, "Apply with resume", over the form. Both hold a
hidden `input#…accept=".doc,.docx,.pdf"` with an upload button beside it: the
modal's "Upload Resume" and the form's own "Select Resume to Upload". The
inputs tie on every attribute, so NEED_UPLOAD answered "ambiguous" and the
button fallback answered "several upload buttons match" for the hints
'Upload Resume', 'upload resume' and 'Apply with resume dialog upload resume'.
The hint 'Select Resume to Upload' picked the form's button, which the modal
covers ("covered or gone (hit DIV#.modal-dialog)"), and the agent ran out of
upload requests with no resume attached.

Fixed: an input or upload button behind an open modal that covers the page is
not reachable, so when inputs tie, the one inside the modal wins, and the
button scan skips buttons behind it. With no modal open nothing changes.

Replica of the page's markup and modal CSS as read from the live page in a
scratch Chromium, served by a local server that records uploads, in a headless
Chromium on a random port (never 9333). The driver loop is the real one with a
scripted fake agent.

On origin/main: every hint 2521's agent sent is refused.

Run: bash scripts/run_regression_tests.sh test_paylocity_modal_upload
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver, uploader  # noqa: E402

JOB_ID = 2521
# Every hint 2521's agent sent, in order, then the hint that worked on 3048.
LIVE_HINTS = ("Upload Resume", "upload resume", "Apply with resume dialog upload resume",
              "Select Resume to Upload", '"Upload Resume" button in Apply with resume dialog', "")

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def guarded(name, fn):
    try:
        fn()
    except Exception as e:
        check(f"{name} (raised {type(e).__name__}: {str(e)[:160]})", False)


# --- replica -----------------------------------------------------------------------

# ?modal=closed: the modal's markup stays in the page, display:none (dismissed).
# ?banner=1: plus a small cookie banner with role=dialog, which covers nothing.
PAGE = r"""<!doctype html><html><head><title>Data Engineer | Paylocity</title>
<style>
  body { margin: 0; font-family: sans-serif; }
  .modal { position: fixed; top: 0; right: 0; bottom: 0; left: 0; z-index: 1000; display: table;
           width: 100%; height: 100%; }
  .modal-dialog { display: table-cell; vertical-align: middle; position: relative; z-index: 1150; }
  .modal-content { position: relative; z-index: 1200; background: #fff; width: 420px; margin: auto;
                   border: 1px solid #999; padding: 16px; }
  .rounded-box { margin: 120px 40px; padding: 16px; border: 1px solid #ccc; }
  .full-width { width: 300px; height: 36px; }
  #banner { position: fixed; left: 0; right: 0; bottom: 0; height: 70px; background: #eee; z-index: 2000; }
</style></head><body class="citrus modal-open">
<h1>Data Engineer</h1>
<form id="apply">
  <div class="rounded-box padding shadow box-shadow-gray-5px "><div class="col-sm-6 col-xs-12">
    <div class="col-xs-12 no-horizontal-padding" id="pageSlot"><input accept=".doc,.docx,.pdf" id="btn-resume" type="file" style="display: none;"><button type="button" class="button branding-button-color full-width" data-automation-id="btn-resume"><span class="undefined">Select Resume to Upload</span></button></div></div>
    <div class="col-sm-6 col-xs-12"><input id="useAttachedResumeToFillOutApplication" type="checkbox" checked=""><label for="useAttachedResumeToFillOutApplication">Fill out application with my resume</label></div></div>
  <h2>Address</h2>
  <label>Street <input name="street"></label>
</form>
<div id="mount"></div>
<script>
  const q = new URLSearchParams(location.search);
  const st = window.__st = {clicks: {}, picked: {}};
  {
    const shown = q.get('modal') === 'closed' ? 'none' : 'table';
    document.getElementById('mount').innerHTML = `<div class="modal in" tab-index="-1" role="dialog" aria-labelledby="myModalLabel" id="0citrus-modal-wrapper" aria-hidden="false" style="display:${shown};z-index:505;"><div class="modal-dialog"><div aria-hidden="true" aria-labelledby="Force Upload Resume" class="modal" id="forceUploadResumeModal" role="dialog" tabindex="-1"><div class="modal-dialog"><div class="modal-content size-small branding-font"><div class="modal-header text-align-center"><h2>Apply with resume</h2></div><div class="modal-body text-align-center" style="overflow-y: visible;"><p>Upload your resume and we'll automatically fill out your application</p></div><div class="modal-footer"><div class="col-xs-12 no-horizontal-padding"><input accept=".doc,.docx,.pdf" id="btn-forceResume" type="file" style="display: none;"><button class="button branding-button-color full-width" data-automation-id="btn-forceResume"><span class="undefined">Upload Resume</span></button></div></div></div></div></div></div></div>`;
  }
  if (q.get('banner')) {
    document.body.insertAdjacentHTML('beforeend', '<div id="banner" role="dialog" aria-label="Cookie banner">We use cookies. <button type="button">Accept All Cookies</button></div>');
  }
  const wire = (slot, inputId) => {
    const inp = document.getElementById(inputId); if (!inp) return;
    inp.parentElement.querySelector('button').addEventListener('click', (e) => {
      e.preventDefault(); st.clicks[slot] = (st.clicks[slot] || 0) + 1; inp.click(); });
    inp.addEventListener('change', async () => {
      const f = inp.files[0]; if (!f) return;
      st.picked[slot] = f.name;
      await fetch('/Recruiting/Jobs/FileUpload/?slot=' + slot + '&name=' + encodeURIComponent(f.name), {method: 'POST', body: f});
      const w = document.getElementById('0citrus-modal-wrapper'); if (w) w.remove();
      document.body.classList.remove('modal-open');
      document.getElementById('pageSlot').innerHTML = '<span>' + f.name + '</span> <button type="button">Remove</button>';
    });
  };
  wire('force', 'btn-forceResume'); wire('page', 'btn-resume');
</script></body></html>"""


class Server:
    def __init__(self):
        self.uploads: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._send(200, PAGE)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                u = urlsplit(self.path)
                if u.path == "/Recruiting/Jobs/FileUpload/":
                    qs = parse_qs(u.query)
                    outer.uploads.append({"slot": qs.get("slot", [""])[0], "name": qs.get("name", [""])[0]})
                self._send(200, "{}", "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class Page:
    def __init__(self, cdp: str):
        self.cdp = cdp

    def _ws(self):
        pages = [t for t in json.load(urllib.request.urlopen(self.cdp + "/json/list", timeout=5))
                 if t["type"] == "page"]
        return uploader._Cdp(pages[0]["webSocketDebuggerUrl"])

    def eval(self, expr):
        with self._ws() as c:
            msg = c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return ((msg.get("result") or {}).get("result") or {}).get("value")

    def goto(self, url, settle=0.5):
        with self._ws() as c:
            c.call("Page.enable")
            c.call("Page.navigate", {"url": url})
        for _ in range(50):
            time.sleep(0.1)
            if self.eval("document.readyState") == "complete" and url in (self.eval("location.href") or ""):
                break
        time.sleep(settle)

    def state(self) -> dict:
        return json.loads(self.eval("JSON.stringify(window.__st || {})") or "{}")

    def buttons(self, hint: str) -> list[str]:
        """The chooser path's candidate buttons, as it scans them."""
        with self._ws() as c:
            c.call("Runtime.enable")
            doc = c.call("Runtime.evaluate", {"expression": "document"})["result"]["result"]["objectId"]
            msg = c.call("Runtime.callFunctionOn", {
                "objectId": doc, "functionDeclaration": uploader._CHOOSER_CONTROLS_JS,
                "arguments": [{"value": "resume"}, {"value": hint}], "returnByValue": True})
        return [x["label"] for x in json.loads(msg["result"]["result"]["value"])]


def by_id(targets, input_id):
    return next(t for t in targets if t.attrs.get("id") == input_id)


# --- live replica --------------------------------------------------------------------


def test_select(srv, page, base):
    page.goto(base + "/Recruiting/Jobs/Apply/1")
    for hint in LIVE_HINTS:
        targets = uploader.find_file_inputs(page.cdp, hint)
        try:
            got = uploader._select(targets, "x.pdf", "resume", hint).attrs.get("id")
        except RuntimeError as e:
            got = f"refused: {e}"
        check(f"hint {hint!r}: picks the modal's input#btn-forceResume", got == "btn-forceResume", got)
    targets = uploader.find_file_inputs(page.cdp, "")
    check("the form's input is marked behind the modal, the modal's is not",
          uploader._BEHIND_MODAL in by_id(targets, "btn-resume").context
          and uploader._BEHIND_MODAL not in by_id(targets, "btn-forceResume").context,
          [t.context[-50:] for t in targets])
    labels = page.buttons("Select Resume to Upload")
    check("button scan: only the modal's Upload Resume, never the covered form button",
          len(labels) == 1 and "upload resume" in labels[0] and "select resume" not in labels[0], labels)
    check("selection uploads nothing", not srv.uploads, srv.uploads)


def test_upload(srv, page, base, named):
    for hint in ("Upload Resume", "Select Resume to Upload"):
        srv.uploads.clear()
        page.goto(base + "/Recruiting/Jobs/Apply/1")
        r = uploader.upload_file(page.cdp, str(named), want="resume", hint=hint)
        check(f"upload, hint {hint!r}: UPLOAD_DONE into the modal's input, one upload, modal gone",
              r.ok and 'id="btn-forceResume"' in r.detail
              and srv.uploads == [{"slot": "force", "name": named.name}]
              and page.eval("!document.getElementById('0citrus-modal-wrapper')"),
              f"{r.ok} {r.detail[:200]} {srv.uploads}")


def test_no_modal(srv, page, base, named):
    # Modal closed: nothing is behind anything, so the GHO-100 behaviour
    # stands: the hidden tie goes to the one rendered button, the form's own.
    for q in ("?modal=closed", "?modal=closed&banner=1"):
        page.goto(base + "/Recruiting/Jobs/Apply/1" + q)
        targets = uploader.find_file_inputs(page.cdp, "")
        check(f"{q}: no input is marked behind a modal",
              len(targets) == 2 and not any(uploader._BEHIND_MODAL in t.context for t in targets),
              [t.context[-50:] for t in targets])
        labels = page.buttons("")
        check(f"{q}: the form's own button is the only upload button",
              len(labels) == 1 and "select resume to upload" in labels[0], labels)
    srv.uploads.clear()
    page.goto(base + "/Recruiting/Jobs/Apply/1?modal=closed&banner=1")
    r = uploader.upload_file(page.cdp, str(named), want="resume", hint="Select Resume to Upload")
    check("modal closed, small cookie banner: UPLOAD_DONE into the form's own input",
          r.ok and srv.uploads == [{"slot": "page", "name": named.name}], f"{r.ok} {r.detail[:200]} {srv.uploads}")


def test_driver(srv, page, base, cdp, resume, named):
    """2521's first request through the real driver loop."""
    srv.uploads.clear()
    page.goto(base + "/Recruiting/Jobs/Apply/1")
    seen: list[str] = []
    rounds = ["NEED_UPLOAD: resume | Upload Resume",
              "STATUS: stuck\nDETAIL: stopped after the upload\nFIELDS_FILLED_JSON: {}\n"]

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        seen.append(prompt)
        return "session_id: s1\n" + rounds[min(len(seen) - 1, len(rounds) - 1)], False

    saved = (agent_driver._run_hermes, agent_driver._dump_debug_transcript, agent_driver.session_api_calls)
    agent_driver._run_hermes = run_hermes
    agent_driver._dump_debug_transcript = lambda *a, **k: None
    agent_driver.session_api_calls = lambda *a, **k: 1
    try:
        agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url=cdp,
                                     job_id=JOB_ID, resume_path=str(resume))
    finally:
        agent_driver._run_hermes, agent_driver._dump_debug_transcript, agent_driver.session_api_calls = saved
    reply = seen[1] if len(seen) > 1 else ""
    check("driver: NEED_UPLOAD 'Upload Resume' is answered UPLOAD_DONE with the modal's input",
          reply.startswith("UPLOAD_DONE") and srv.uploads == [{"slot": "force", "name": named.name}],
          f"{reply[:200]} {srv.uploads}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-paylocity-modal-", ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        os.environ["JOBO_UPLOAD_COPY_DIR"] = str(tmp / "copies")
        resume = tmp / "artifacts" / str(JOB_ID) / "resume.pdf"
        resume.parent.mkdir(parents=True)
        resume.write_bytes(b"%PDF-1.4\n% jobo paylocity modal regression\n" + os.urandom(64) + b"\n%%EOF\n")
        from module4 import upload_names
        named = Path(upload_names.prepare(JOB_ID, str(resume)))

        srv = Server()
        profile = tmp / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + str(profile),
             f"--host-resolver-rules=MAP recruiting.paylocity.example 127.0.0.1:{srv.port}, MAP * ~NOTFOUND, "
             "EXCLUDE localhost, EXCLUDE 127.0.0.1",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = profile / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists() and portfile.read_text().strip():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            page = Page(cdp)
            base = f"http://recruiting.paylocity.example:{srv.port}"
            guarded("select", lambda: test_select(srv, page, base))
            guarded("upload", lambda: test_upload(srv, page, base, named))
            guarded("no modal", lambda: test_no_modal(srv, page, base, named))
            guarded("driver", lambda: test_driver(srv, page, base, cdp, resume, named))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            srv.httpd.shutdown()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
