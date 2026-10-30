#!/usr/bin/env python3
"""A CV dialog with two hidden twin inputs, and its Cancel that sends (GHO-100).

Run 2026-10-30 10:00 IST, Wren 2909 on Duruper (duruper.com): after an
email-OTP sign-up, Apply opened an "Upload Your CV" dialog with two buttons,
"AI CV Upload" and "Manual CV Upload", and two bare `display:none`
`<input type=file accept=".pdf,.doc,.docx,.txt">` side by side under one div.
Nothing on the inputs tells them apart, so NEED_UPLOAD answered "ambiguous
upload slots" twice, even with the hint `under "Manual CV Upload"`. The agent
then pressed the dialog's Cancel, and that SENT the application with no CV.

Fixed:
  * uploader: when the DOM tie is between inputs none of which is rendered,
    the button the hint names is clicked with the file chooser intercepted, so
    the site itself says which input it opens. A tie between buttons is still
    refused (no click), and rendered twins still fail as before.
  * guard: after an UPLOAD_FAILED of this job's resume, Cancel/Close/Skip/
    Escape on an open dialog holding a resume field is refused, both for
    NEED_CLICK and for the agent's own browser_click (Hermes plugin veto).

Replica of the dialog from the agent's own snapshots and console reads (Hermes
session DB), served by a local server that records uploads and submissions,
in a headless Chromium on a random port (never 9333). The driver loop is the
real one with a scripted fake agent.

On origin/main: every upload request is "ambiguous", and Cancel goes through
and sends the application.

Run: bash scripts/run_regression_tests.sh test_cv_dialog_upload
"""
from __future__ import annotations

import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver, prompts, uploader  # noqa: E402

PLUGIN_DIR = ROOT / "hermes_plugin" / "jobo-browser"
JOB_ID = 2909

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def guarded(name, fn):
    try:
        fn()
    except Exception as e:
        check(f"{name} (raised {type(e).__name__}: {str(e)[:160]})", False)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- replica -----------------------------------------------------------------------

# ?inputs=shown renders the twins (no display:none): those must still fail.
# ?dialog=cookie is a dialog with no upload at all: closing it is harmless.
PAGE = r"""<!doctype html><html><head><title>Software Engineer | Duruper</title></head><body>
<main>
  <h1>Software Engineer</h1>
  <div id="dlg" role="dialog" aria-modal="true" aria-labelledby="dlgTitle"
       style="position:fixed;inset:30px;background:#fff;border:1px solid #333;padding:16px">
    <button id="close" type="button" aria-label="Close">&#10005;</button>
    <div id="body"></div>
    <button id="cancel" type="button">Cancel</button>
  </div>
</main>
<script>
  const q = new URLSearchParams(location.search);
  const st = window.__st = {clicks: {}, picked: {}};
  const body = document.getElementById('body');
  const send = () => { fetch('/apply', {method: 'POST'}).then(() => document.getElementById('dlg').remove()); };
  document.getElementById('cancel').onclick = send;
  document.getElementById('close').onclick = send;
  if (q.get('dialog') === 'cookie') {
    body.innerHTML = '<h2 id="dlgTitle">Cookie settings</h2><p>We use cookies.</p>';
  } else {
    const hide = q.get('inputs') === 'shown' ? '' : ' style="display: none;"';
    body.innerHTML = `
      <div><span>CV checkpoint</span><h2 id="dlgTitle">Upload Your CV</h2>
        <p>By uploading a CV it will automatically be added to all applications that do not have a CV.</p></div>
      <div class="relative flex flex-col gap-4">
        <button id="ai" type="button"><span>AI CV Upload</span>
          <span>AI automatically analyzes to complete your profile</span>
          <span>AI parsing is limited to 3 attempts per day.</span><span>SMART</span><span>3X FASTER</span><span>&rsaquo;</span></button>
        <button id="manual" type="button"><span>Manual CV Upload</span>
          <span>Upload your CV file directly to your profile</span><span>MANUAL</span><span>&rsaquo;</span></button>
        <input accept=".pdf,.doc,.docx,.txt" type="file"${hide}>
        <input accept=".pdf,.doc,.docx,.txt" type="file"${hide}>
      </div>
      <div id="chip"></div>`;
    const [aiIn, manIn] = document.querySelectorAll('input[type=file]');
    const opens = (id, inp) => document.getElementById(id).addEventListener('click', () => {
      st.clicks[id] = (st.clicks[id] || 0) + 1; inp.click(); });
    opens('ai', aiIn); opens('manual', manIn);
    const takes = (slot, inp) => inp.addEventListener('change', async () => {
      const f = inp.files[0]; if (!f) return;
      st.picked[slot] = f.name;
      await fetch('/cv?slot=' + slot + '&name=' + encodeURIComponent(f.name), {method: 'POST', body: f});
      document.getElementById('chip').textContent = f.name + ' uploaded';
    });
    takes('ai', aiIn); takes('manual', manIn);
  }
</script></body></html>"""


class Server:
    def __init__(self):
        self.uploads: list[dict] = []
        self.submits: list[dict] = []
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
                from urllib.parse import parse_qs, urlsplit
                u = urlsplit(self.path)
                if u.path == "/cv":
                    qs = parse_qs(u.query)
                    outer.uploads.append({"slot": qs.get("slot", [""])[0], "name": qs.get("name", [""])[0]})
                elif u.path == "/apply":
                    outer.submits.append({"cv": [x["name"] for x in outer.uploads]})
                self._send(200, "{}", "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.uploads.clear()
        self.submits.clear()


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

    def goto(self, url, settle=0.6):
        with self._ws() as c:
            c.call("Page.enable")
            c.call("Page.navigate", {"url": url})
        for _ in range(50):
            time.sleep(0.1)
            if self.eval("document.readyState") == "complete" and url.split("?")[0] in (self.eval("location.href") or ""):
                break
        time.sleep(settle)

    def state(self) -> dict:
        return json.loads(self.eval("JSON.stringify(window.__st || {})") or "{}")


VERDICT = "STATUS: submitted\nDETAIL: Application submitted.\nFIELDS_FILLED_JSON: {}\n"


def drive(cdp: str, resume: Path, rounds: list):
    """agent_driver.run_apply_agent with scripted agent turns (see test_external_resume_gate)."""
    seen: list[str] = []

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        seen.append(prompt)
        i = len(seen) - 1
        r = rounds[i] if i < len(rounds) else VERDICT
        out = r(prompt) if callable(r) else r
        return "session_id: s1\n" + out, False

    saved = (agent_driver._run_hermes, agent_driver._dump_debug_transcript, agent_driver.session_api_calls)
    agent_driver._run_hermes = run_hermes
    agent_driver._dump_debug_transcript = lambda *a, **k: None
    agent_driver.session_api_calls = lambda *a, **k: 1
    try:
        out = agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url=cdp,
                                           job_id=JOB_ID, resume_path=str(resume))
    finally:
        agent_driver._run_hermes, agent_driver._dump_debug_transcript, agent_driver.session_api_calls = saved
    return out, seen


# --- offline -----------------------------------------------------------------------


def test_rule():
    g = _load("rg_dismiss_offline", PLUGIN_DIR / "resume_guard.py")
    ours = f"Alex_Example_Resume_{JOB_ID}_abc123.pdf"
    in_dlg = {"external": True, "slots": [{"how": "upload button", "desc": "Manual CV Upload", "in_dialog": True}]}
    on_page = {"external": True, "slots": [{"how": "file input", "desc": "Resume", "in_dialog": False}]}
    failed = {"resume_upload_failed": "2 file input(s) found; ambiguous upload slots"}
    rows = [
        ("upload failed, CV dialog open: refuse", [in_dlg], failed, False),
        ("no upload failed yet: allow", [in_dlg], {}, True),
        ("a later upload verified: allow", [in_dlg], {**failed, "resume_uploads_ok": 1}, True),
        ("page shows our file: allow", [{**in_dlg, "shown_ours": True}], failed, True),
        ("resume field on the page, not in a dialog: allow", [on_page], failed, True),
        ("no page read: allow", [], failed, True),
    ]
    for name, states, guard, want in rows:
        ok, reason = g.judge_dismiss(states, ours, guard)
        check(f"judge_dismiss: {name}", ok is want, reason)
    names = {"Cancel": True, "Close": True, "✕": True, "Skip for now": True, "Not now": True,
             "Manual CV Upload": False, "Close job details": True, "Submit": False, "Closed roles": False, "button Cancel": True, "button Manual CV Upload": False}
    for label, want in names.items():
        check(f"dismiss name {label!r} -> {want}", bool(g.DISMISS_NAME.search(label)) is want)


def test_prompt():
    text = prompts.SYSTEM_PROMPT
    check("prompt: never close/cancel/skip an upload dialog after UPLOAD_FAILED",
          "never close, cancel, skip or" in text and "SENDS the application without the file" in text)
    check("prompt: hidden inputs behind buttons are named by the button's text",
          "NEED_UPLOAD: resume | Manual CV Upload" in text)


def test_messages():
    r = uploader.UploadResult(False, "x", candidates=["manual cv upload", "ai cv upload"], buttons=True)
    check("a button tie lists the buttons and asks for the button's text",
          "Upload buttons that WERE found" in r.as_agent_message() and "Manual CV Upload" in r.as_agent_message())
    r = uploader.UploadResult(False, "x", candidates=['accept=".pdf"'] * 2, hidden_tie=True)
    check("an unrescued hidden tie says a visible button opens one",
          "a visible upload button opens one" in r.as_agent_message())


# --- live replica --------------------------------------------------------------------


def test_uploader(srv, page, base, named):
    srv.reset()
    page.goto(base + "/job")
    r = uploader.upload_file(page.cdp, str(named), want="resume")
    st = page.state()
    check("no hint: refused (two buttons tie), nothing clicked, nothing uploaded",
          not r.ok and not st.get("clicks") and not srv.uploads and r.buttons
          and any("manual cv upload" in c for c in r.candidates), f"{r.detail[:200]} {st}")
    check("no hint: the reply lists both buttons for the agent",
          "Upload buttons that WERE found" in r.as_agent_message(), r.as_agent_message()[:300])

    for hint in ('under "Manual CV Upload"', "Manual CV Upload"):
        srv.reset()
        page.goto(base + "/job")
        r = uploader.upload_file(page.cdp, str(named), want="resume", hint=hint)
        st = page.state()
        check(f"hint {hint!r}: UPLOAD_DONE through the Manual button's own input",
              r.ok and st.get("picked") == {"manual": named.name} and st.get("clicks") == {"manual": 1}
              and srv.uploads == [{"slot": "manual", "name": named.name}] and not srv.submits,
              f"{r.ok} {r.detail[:200]} {st} {srv.uploads}")

    srv.reset()
    page.goto(base + "/job?inputs=shown")
    r = uploader.upload_file(page.cdp, str(named), want="resume", hint="Manual CV Upload")
    st = page.state()
    check("rendered twins: still ambiguous, no button clicked (no guessing)",
          not r.ok and "ambiguous upload slots" in r.detail and not st.get("clicks") and not srv.uploads,
          f"{r.detail[:200]} {st}")


def test_driver(cdp, srv, page, base, resume, named):
    # Bad path, 2909's: the upload fails, then the agent asks for Cancel.
    srv.reset()
    page.goto(base + "/job")
    st: dict = {}

    def cancel(prompt):
        st["upload"] = prompt
        return "NEED_CLICK: Cancel"

    def note(prompt):
        st["refusal"] = prompt
        return "STATUS: stuck\nDETAIL: upload:failed in the Upload Your CV dialog\nFIELDS_FILLED_JSON: {}\n"

    out, _ = drive(cdp, resume, ["NEED_UPLOAD: resume", cancel, note])
    check("driver: the hintless request is UPLOAD_FAILED with the button list",
          st.get("upload", "").startswith("UPLOAD_FAILED") and "Upload buttons" in st.get("upload", ""),
          st.get("upload", "")[:200])
    check("driver: NEED_CLICK Cancel on the CV dialog after UPLOAD_FAILED is REFUSED, nothing sent",
          "not closing this upload dialog" in st.get("refusal", "") and not srv.submits,
          f"{st.get('refusal', '')[:200]} submits={srv.submits}")
    check("driver: the job ends stuck, not submitted", out.status == "stuck", out.status)

    # Good path: the agent names the Manual button, then the dialog goes.
    srv.reset()
    page.goto(base + "/job")

    def retry(prompt):
        return 'NEED_UPLOAD: resume | under "Manual CV Upload"'

    def done(prompt):
        st["done"] = prompt
        return "NEED_CLICK: Cancel"

    out, _ = drive(cdp, resume, ["NEED_UPLOAD: resume", retry, done])
    time.sleep(0.5)
    check("driver good path: UPLOAD_DONE, one submission carrying this job's CV, no violation",
          st.get("done", "").startswith("UPLOAD_DONE") and srv.submits == [{"cv": [named.name]}]
          and not out.resume_rule_violation,
          f"{st.get('done', '')[:160]} {srv.submits} {out.resume_rule_violation}")


def test_plugin_veto(cdp, srv, page, base, named, tmp: Path):
    plugin = _load("jobo_browser_plugin_cv_dialog", PLUGIN_DIR / "__init__.py")
    state = tmp / "guard_state.json"
    env = {"BROWSER_CDP_URL": cdp, "JOBO_UPLOAD_RESUME_NAME": named.name, "JOBO_RESUME_GUARD_FILE": str(state)}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        srv.reset()
        page.goto(base + "/job")
        plugin.account_guard.remember_refs('- button "Close" [ref=e2]\n- button "Cancel" [ref=e1]\n'
                                           '- button "Manual CV Upload Upload your CV file directly" [ref=e4]')
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 0,
                                     "resume_upload_failed": "2 file input(s) found; ambiguous upload slots"}))
        for ref, what in (("@e1", "Cancel"), ("e2", "Close")):
            b = plugin._pre_tool_call("browser_click", {"ref": ref})
            check(f"plugin veto: browser_click on {what!r} of the CV dialog after UPLOAD_FAILED is BLOCKED (2909's path)",
                  (b or {}).get("action") == "block" and "not closing this upload dialog" in b.get("message", ""),
                  str(b)[:200])
        b = plugin._pre_tool_call("browser_press", {"key": "Escape"})
        check("plugin veto: Escape is BLOCKED too", (b or {}).get("action") == "block", str(b)[:120])
        b = plugin._pre_tool_call("browser_console", {"expression": "document.getElementById('cancel').click()"})
        check("plugin veto: a console script clicking Cancel is BLOCKED", (b or {}).get("action") == "block")
        check("plugin veto: the Manual CV Upload button itself is not blocked",
              plugin._pre_tool_call("browser_click", {"ref": "e4"}) is None)
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 1, "resume_upload_failed": ""}))
        check("plugin veto lifts once the driver verified an upload",
              plugin._pre_tool_call("browser_click", {"ref": "e1"}) is None)
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 0, "resume_upload_failed": ""}))
        check("plugin veto: no failed upload yet, Cancel passes",
              plugin._pre_tool_call("browser_click", {"ref": "e1"}) is None)
        page.goto(base + "/job?dialog=cookie")
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 0,
                                     "resume_upload_failed": "2 file input(s) found; ambiguous upload slots"}))
        check("plugin veto: closing a dialog with no resume field is never blocked",
              plugin._pre_tool_call("browser_click", {"ref": "e2"}) is None)
        check("nothing was submitted by any guard check", not srv.submits, str(srv.submits))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-cvdialog-", ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        os.environ["JOBO_UPLOAD_COPY_DIR"] = str(tmp / "copies")
        resume = tmp / "artifacts" / str(JOB_ID) / "resume.pdf"
        resume.parent.mkdir(parents=True)
        resume.write_bytes(b"%PDF-1.4\n% jobo cv dialog regression\n" + os.urandom(64) + b"\n%%EOF\n")
        from module4 import upload_names
        named = Path(upload_names.prepare(JOB_ID, str(resume)))

        guarded("rule", test_rule)
        guarded("prompt", test_prompt)
        guarded("messages", test_messages)

        srv = Server()
        profile = tmp / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,1000", "--user-data-dir=" + str(profile),
             f"--host-resolver-rules=MAP www.duruper.example 127.0.0.1:{srv.port}, MAP * ~NOTFOUND, "
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
            base = f"http://www.duruper.example:{srv.port}"
            guarded("uploader", lambda: test_uploader(srv, page, base, named))
            guarded("driver", lambda: test_driver(cdp, srv, page, base, resume, named))
            guarded("plugin veto", lambda: test_plugin_veto(cdp, srv, page, base, named, tmp))
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
