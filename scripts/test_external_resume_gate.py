#!/usr/bin/env python3
"""No external form is submitted without THIS job's resume verified (GHO-70).

Overnight run 2026-10-21 23:50 IST: two applications went out with no
verified upload of the job's resume, and only the after-the-fact
"RESUME RULE VIOLATION" line noticed:

  * Trey 272, Oracle Cloud Recruiting "Welcome Back" page. Our write to
    input#quickProfileImport ("Import your profile from resume") made Oracle
    parse the file, store it and jump to the next step. The input was gone and
    no file name showed, so the upload read "upload input disappeared after
    the write"; a second NEED_UPLOAD found no input; NEED_CLICK SUBMIT went
    straight through.
  * Margie 1635, TurboHire. The "Upload Resume" slot took the file and
    showed it cut short as "Alex_Example_Re...", which the full-name check
    never matched. The agent then sent the form with a direct browser_click
    on "Apply For This Job".

Replicas of both (from the agent's own snapshots in the Hermes session DB),
plus a form with no resume field at all, are served by a local server that
records uploads and submissions, in a headless Chromium on a random port
(never 9333). The driver loop is the real one with a scripted fake agent; the
Hermes plugin's pre_tool_call hook is called the way Hermes calls it.

On origin/main: both uploads fail, and every bad-path submit goes through.

GHO-78 (Verizon 2219, 2026-10-22): attempt 2 resumed a Workday draft that kept
attempt 1's upload of this job's file and sent the Review page with a direct
browser_click on "Submit". The veto saw our file and allowed it, but the run
still logged RESUME RULE VIOLATION because only NEED_CLICK recorded the
proof. Replica: the Review step from that session's snapshot.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver, prompts, uploader  # noqa: E402

PLUGIN_DIR = ROOT / "hermes_plugin" / "jobo-browser"
JOB_ID = 1635

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


# --- replicas --------------------------------------------------------------------

# TurboHire's application dialog (margie.turbohire.co, 2026-10-22 03:15):
# an "Autofill With Resume" import slot, the real "Upload Resume *" slot, a
# privacy checkbox and Next; step 2 ends in "Apply For This Job". On change
# the slot drops its input and shows the name cut to 15 characters + "...".
# ?broken=1: the write is lost (input dropped, nothing sent, nothing shown).
TURBOHIRE = r"""<!doctype html><title>Margie - TurboHire</title>
<div role="dialog" id="dlg"><h2>Data Scientist / AI Engineer</h2>
<div id="step1">
  <p>AUTOFILL APPLICATION</p><p>Import resume to auto-fill the data</p>
  <div><button type="button">Autofill With Resume</button><input type="file" id="autofill" accept=".pdf"></div>
  <label>First Name * <input id="fn"></label>
  <div id="resumeslot"><span>Upload Resume</span><span> *</span> <span id="chip"></span>
    <input type="file" id="resume" accept=".doc, .docx, .pdf"></div>
  <label><input type="checkbox" id="privacy"> I have read and understood TurboHire's Privacy Policy*</label>
  <button type="button" id="next">Next</button>
</div>
<div id="step2" hidden><label>Notice period <input id="notice"></label>
  <button type="button" id="apply">Apply For This Job</button></div>
<div id="done" hidden><h4>Congratulations!</h4></div>
</div>
<script>
const q = new URLSearchParams(location.search);
let uploaded = null;
document.getElementById('resume').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  e.target.remove();
  if (q.get('broken')) return;
  const fd = new FormData(); fd.append('file', f, f.name);
  const r = await fetch('/api/upload', {method: 'POST', body: fd});
  if (r.ok) { uploaded = f.name; document.getElementById('chip').textContent = f.name.slice(0, 15) + '...'; }
});
document.getElementById('next').onclick = () => {
  document.getElementById('step1').hidden = true; document.getElementById('step2').hidden = false; };
document.getElementById('apply').onclick = async () => {
  await fetch('/api/submit', {method: 'POST', body: JSON.stringify({resume: uploaded})});
  document.getElementById('step2').hidden = true; document.getElementById('done').hidden = false; };
</script>"""

# Oracle Cloud Recruiting "Welcome Back" quick apply (Trey CX_2, 2026-10-22
# 03:03): the import input parses the file, stores it as the draft's
# attachment, then the page becomes the "You're almost there" step, which
# never shows the file. ?parseonly=1: the site parses but stores nothing.
ORACLE = r"""<!doctype html><title>Trey Careers</title>
<main><form id="f" onsubmit="return false">
  <h1>Welcome Back Alex</h1>
  <p>We have the profile you shared on 8/27/2026. You can use it to apply for this job.
     You can also use your LinkedIn or Indeed profile, or upload your resume.</p>
  <button type="button" id="useprofile">USE PROFILE FROM 8/27/2026.</button>
  <div class="quick-apply-flow-profile-import-awli"><input type="file" id="quickProfileImport"
    aria-label="Import your profile from resume" accept=".doc,.docx,.odt,.pdf,.txt,.rtf"
    class="quick-apply-flow-profile-import-awli__file-upload"></div>
</form></main>
<script>
const q = new URLSearchParams(location.search);
let stored = null;
const API = '/hcmRestApi/resources/latest/';
document.getElementById('quickProfileImport').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const send = (path) => { const fd = new FormData(); fd.append('file', f, f.name);
    return fetch(API + path, {method: 'POST', body: fd}); };
  await send('recruitingCECandidateResumeParsers');
  if (!q.get('parseonly')) {
    const r = await send('recruitingCEJobApplicationDrafts/300005684805735/child/attachments');
    if (r.ok) stored = f.name;
  }
  // Same <form> element, new contents (as on the live page: the run read
  // "input disappeared", not "form replaced").
  document.getElementById('f').innerHTML =
    '<h1>Authentication screen. You\'re almost there, Alex.</h1>' +
    '<label>Address Line 1 <input id="addr"></label>' +
    '<button type="button" id="submit">SUBMIT</button>';
  document.getElementById('submit').onclick = () =>
    fetch('/api/submit', {method: 'POST', body: JSON.stringify({resume: stored})});
});
</script>"""

# Workday's Review step on a resumed draft (Verizon 2219, 2026-10-22 20:11):
# the "Resume/CV" group lists the draft's file as text, no upload control.
# ?file=<name> picks the file the draft holds; none = no file at all.
WORKDAY_REVIEW = r"""<!doctype html><title>Workday</title>
<main><h2>Junior Data Security Engineer</h2><h3>Review</h3>
<div role="group" aria-label="Resume/CV" id="cv"><h4>Resume/CV</h4></div>
<button type="button">Back</button><button type="button" id="submit">Submit</button></main>
<script>
const f = new URLSearchParams(location.search).get('file');
if (f) document.getElementById('cv').insertAdjacentHTML('beforeend',
  '<div role="alert">' + f + ' successfully uploaded</div><span>' + f + '</span><span>102.21 KB</span>');
document.getElementById('submit').onclick = () =>
  fetch('/api/submit', {method: 'POST', body: JSON.stringify({resume: f})});
</script>"""

# A form that genuinely has no resume field.
PLAIN = r"""<!doctype html><title>Acme quick form</title>
<form onsubmit="return false"><h1>Apply</h1><label>Full name <input id="name"></label>
<button type="button" id="go" onclick="fetch('/api/submit',{method:'POST',body:JSON.stringify({resume:null})})">Submit application</button>
</form>"""


class Server:
    def __init__(self):
        self.uploads: list[dict] = []
        self.submits: list[dict] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                page = {"/turbohire": TURBOHIRE, "/oracle": ORACLE, "/plain": PLAIN,
                        "/workday": WORKDAY_REVIEW}.get(path)
                self._send(200, page) if page else self._send(404, "nope")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path.split("?", 1)[0]
                if path == "/api/submit":
                    srv.submits.append(json.loads(body or b"{}"))
                    self._send(200, "{}", "application/json")
                    return
                msg = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + (self.headers.get("Content-Type") or "").encode() + b"\r\n\r\n" + body)
                for part in msg.iter_parts() if msg.is_multipart() else []:
                    if part.get_filename():
                        data = part.get_payload(decode=True)
                        srv.uploads.append({"path": path, "name": part.get_filename(),
                                            "sha256": hashlib.sha256(data).hexdigest()})
                        self._send(201 if path.startswith("/hcmRestApi") else 200, "{}", "application/json")
                        return
                self._send(400, "no file")

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


VERDICT = "STATUS: submitted\nDETAIL: Application submitted.\nFIELDS_FILLED_JSON: {}\n"


def drive(cdp: str, resume: Path, rounds: list, job_id: int = JOB_ID):
    """agent_driver.run_apply_agent with scripted agent turns (see test_easy_apply_resume)."""
    prompts_seen: list[str] = []

    def run_hermes(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        prompts_seen.append(prompt)
        i = len(prompts_seen) - 1
        r = rounds[i] if i < len(rounds) else VERDICT
        out = r(prompt) if callable(r) else r
        return "session_id: s1\n" + out, False

    saved = (agent_driver._run_hermes, agent_driver._dump_debug_transcript, agent_driver.session_api_calls)
    agent_driver._run_hermes = run_hermes
    agent_driver._dump_debug_transcript = lambda *a, **k: None
    agent_driver.session_api_calls = lambda *a, **k: 1
    try:
        out = agent_driver.run_apply_agent(system_prompt="SYS", user_prompt="TASK", cdp_url=cdp,
                                           job_id=job_id, resume_path=str(resume))
    finally:
        agent_driver._run_hermes, agent_driver._dump_debug_transcript, agent_driver.session_api_calls = saved
    return out, prompts_seen


# --- part 1: offline ---------------------------------------------------------------


def test_rule():
    g = _load("rg_ext_offline", PLUGIN_DIR / "resume_guard.py")
    ours = f"Alex_Example_Resume_{JOB_ID}_abc123.pdf"
    slot = {"slots": [{"how": "file input", "desc": "Upload Resume", "files": []}], "external": True}
    rows = [
        ("verified upload: allow", [slot], {"resume_uploads_ok": 1}, True),
        ("Easy Apply kept our card after an earlier failed upload: allow", [],
         {"verified": True, "resume_upload_failed": "x"}, True),
        ("upload failed, page shows nothing: refuse", [{"external": True}],
         {"resume_upload_failed": "upload input disappeared after the write"}, False),
        ("upload failed but the form shows our file: allow", [{"external": True, "shown_ours": True}],
         {"resume_upload_failed": "x"}, True),
        ("our file held by an input: allow", [{"external": True, "ours_in_input": True}], {}, True),
        ("resume field, nothing uploaded: refuse", [slot], {}, False),
        ("no resume field, nothing attempted: allow", [{"external": True, "slots": []}], {}, True),
        ("no page read at all, nothing attempted: allow", [], {}, True),
        ("page shows another job's file, not ours: refuse",
         [{"external": True, "other_ours": [f"alex_example_resume_1464_def456.pdf"]}], {}, False),
        ("page shows ours next to another job's: allow",
         [{"external": True, "shown_ours": True, "other_ours": ["alex_example_resume_1464_def456.pdf"]}], {}, True),
    ]
    for label, states, gs, want in rows:
        ok, reason = g.judge_external(states, ours, gs)
        check(f"rule: {label}", ok is want, reason)
    msg = g.refusal_message_external("r", ours)
    check("refusal names NEED_UPLOAD: resume and STATUS: stuck, never another way to send",
          "NEED_UPLOAD: resume" in msg and "STATUS: stuck" in msg and "Submit refused" in msg, msg)


def test_audit_redaction():
    from module4 import password_reset, submission_audit

    secret = password_reset.STANDARD_PASSWORD or "Example@123"
    out = submission_audit._redact_passwords({
        "Email (sign in)": "a@example.com", "Password": secret, "Verify New Password": "x",
        "Account": f"created with {secret}", "Resume": "Alex_Example_Resume_1_abc123.pdf"})
    check("audit: the account password is redacted, other fields kept",
          out["Password"] == out["Verify New Password"] == "[redacted]" and secret not in out["Account"]
          and out["Email (sign in)"] == "a@example.com" and out["Resume"].endswith(".pdf"), str(out))


def test_stored_evidence():
    stored = uploader._stored([
        "POST mRestApi/resources/latest/recruitingCECandidateResumeParsers -> HTTP 201",
        "POST tingCEJobApplicationDrafts/300005684805735/child/attachments -> HTTP 201",
        "POST /api/upload (in flight)",
        "POST /x/attachments -> HTTP 400",
    ])
    check("site evidence: the Oracle draft attachment POST counts as stored, the parse does not",
          stored == ["POST tingCEJobApplicationDrafts/300005684805735/child/attachments -> HTTP 201"], str(stored))
    check("site evidence: an autofill/parse upload endpoint is not storage",
          uploader._stored(["POST /api/resume/autofill/upload -> HTTP 200"]) == [])


def test_prompt():
    sp = prompts.SYSTEM_PROMPT
    check("prompt: every form, not only Easy Apply, refuses Submit without the resume verified",
          "Every form, not only Easy Apply" in sp and "Submit refused, resume not verified" in sp)
    check("prompt: an unverifiable resume ends in STATUS: stuck, a form with no resume field submits",
          "do NOT find another way to submit" in sp and "A form with no resume field" in sp)


def test_plugin_parsing():
    plugin = _load("jobo_browser_plugin_ext_t", PLUGIN_DIR / "__init__.py")
    rg = plugin.resume_guard
    plugin.account_guard.remember_refs('- button "Apply For This Job" [ref=e3]\n- button "Next" [ref=e2]')
    ext = lambda t, a: rg._submit_attempt(t, a, plugin._ref_name, rg._EXTERNAL_SUBMIT_NAME)[0]  # noqa: E731
    check("plugin: browser_click on 'Apply For This Job' counts as a submit on other sites",
          ext("browser_click", {"ref": "@e3"}))
    check("plugin: browser_click on Next does not", not ext("browser_click", {"ref": "e2"}))


# --- part 2: replicas in a real browser ------------------------------------------


def test_turbohire_upload(srv, page, base, named):
    srv.reset()
    page.goto(base + "/turbohire")
    res = uploader.upload_file(page.cdp, str(named), want="resume")
    chip = page.eval("document.getElementById('chip').textContent")
    check("TurboHire: the truncated 'Name_Re...' chip after our write verifies the upload",
          res.ok and chip == named.name[:15] + "..." and [u["name"] for u in srv.uploads] == [named.name],
          f"{res.detail[:200]} chip={chip!r} uploads={srv.uploads}")


def test_turbohire_gate(cdp, srv, page, base, resume, named):
    # Bad path: the write is lost, then the agent goes on and submits.
    srv.reset()
    page.goto(base + "/turbohire?broken=1")
    st = {}

    def go_on(prompt):
        st["upload"] = prompt
        page.eval("document.getElementById('next').click()")
        return "NEED_CLICK: Apply For This Job"

    def note(prompt):
        st["refusal"] = prompt
        return "STATUS: stuck\nDETAIL: resume could not be attached\nFIELDS_FILLED_JSON: {}\n"

    out, _ = drive(cdp, resume, ["NEED_UPLOAD: resume", go_on, note])
    check("TurboHire bad path: the lost write is reported as UPLOAD_FAILED",
          st.get("upload", "").startswith("UPLOAD_FAILED"), st.get("upload", "")[:160])
    check("TurboHire bad path: NEED_CLICK 'Apply For This Job' after UPLOAD_FAILED is REFUSED, nothing sent",
          "Submit refused, resume not verified" in st.get("refusal", "") and not srv.submits,
          f"{st.get('refusal', '')[:200]} submits={srv.submits}")

    # Good path: verified upload, Next, Apply For This Job.
    srv.reset()
    page.goto(base + "/turbohire")

    def forward(prompt):
        st["done"] = prompt
        page.eval("document.getElementById('next').click()")
        return "NEED_CLICK: Apply For This Job"

    out, seen = drive(cdp, resume, ["NEED_UPLOAD: resume", forward])
    time.sleep(0.5)
    check("TurboHire good path: UPLOAD_DONE, one submission carrying this job's file, no violation",
          st.get("done", "").startswith("UPLOAD_DONE") and len(srv.submits) == 1
          and srv.submits[0].get("resume") == named.name and not out.resume_rule_violation,
          f"{st.get('done', '')[:120]} {srv.submits} {out.resume_rule_violation}")


def test_plugin_veto(cdp, srv, page, base, named, tmp: Path):
    plugin = _load("jobo_browser_plugin_ext_live", PLUGIN_DIR / "__init__.py")
    state = tmp / "guard_state.json"
    env = {"BROWSER_CDP_URL": cdp, "JOBO_UPLOAD_RESUME_NAME": named.name, "JOBO_RESUME_GUARD_FILE": str(state)}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        srv.reset()
        page.goto(base + "/turbohire?broken=1")
        page.eval("document.getElementById('next').click()")
        plugin.account_guard.remember_refs('- button "Apply For This Job" [ref=e3]\n- button "Next" [ref=e2]')
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 0,
                                     "resume_upload_failed": "upload input disappeared after the write"}))
        blocked = plugin._pre_tool_call("browser_click", {"ref": "@e3"})
        check("plugin veto: direct browser_click on 'Apply For This Job' after UPLOAD_FAILED is BLOCKED (1635's path)",
              (blocked or {}).get("action") == "block" and "Submit refused" in blocked.get("message", ""),
              str(blocked)[:200])
        js = plugin._pre_tool_call("browser_console", {"expression": "document.getElementById('apply').click()"})
        check("plugin veto: a console script clicking it is BLOCKED too", (js or {}).get("action") == "block")
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 1, "resume_upload_failed": ""}))
        check("plugin veto lifts once the driver verified an upload",
              plugin._pre_tool_call("browser_click", {"ref": "e3"}) is None)
        # A resume field on the page and no upload attempted yet.
        page.goto(base + "/turbohire")
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 0, "resume_upload_failed": ""}))
        plugin.account_guard.remember_refs('- button "Submit application" [ref=e7]')
        check("plugin veto: submitting a form whose resume field was never used is BLOCKED",
              (plugin._pre_tool_call("browser_click", {"ref": "e7"}) or {}).get("action") == "block")
        page.goto(base + "/plain")
        check("plugin veto: a form with no resume field is never blocked",
              plugin._pre_tool_call("browser_click", {"ref": "e7"}) is None)
        check("nothing was submitted by any guard check", not srv.submits, str(srv.submits))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_resumed_workday_draft(cdp, srv, page, base, resume, named):
    """GHO-78: a direct browser_click on Submit, no NEED_CLICK, no new upload."""
    plugin = _load("jobo_browser_plugin_ext_wd", PLUGIN_DIR / "__init__.py")
    other = named.name.replace(f"_Resume_{JOB_ID}_", "_Resume_1464_")
    st: dict = {}

    def click_submit(prompt):
        env = {**agent_driver._JOB.env(), "BROWSER_CDP_URL": cdp}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            plugin.account_guard.remember_refs('- button "Back" [ref=e44]\n- button "Submit" [ref=e45]')
            blocked = plugin._pre_tool_call("browser_click", {"ref": "e45"})
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        st["blocked"] = blocked
        if blocked:
            return "STATUS: stuck\nDETAIL: the draft holds another resume\nFIELDS_FILLED_JSON: {}\n"
        page.eval("document.getElementById('submit').click()")
        time.sleep(0.3)
        return VERDICT

    srv.reset()
    page.goto(f"{base}/workday?file={named.name}")
    out, _ = drive(cdp, resume, [click_submit])
    check("resumed Workday draft with this job's file: the direct Submit click goes through",
          st.get("blocked") is None and [x.get("resume") for x in srv.submits] == [named.name],
          f"{st.get('blocked')} {srv.submits}")
    check("...and the run records the page evidence instead of a RESUME RULE VIOLATION (2219)",
          out.status == "submitted" and not out.resume_rule_violation and out.resume_seen_on_page,
          f"{out.status} {out.resume_rule_violation!r} seen={out.resume_seen_on_page}")

    # The next attempt starts clean: the last attempt's evidence must not
    # cover a draft that now holds no resume at all.
    srv.reset()
    page.goto(f"{base}/workday")
    out, _ = drive(cdp, resume, [click_submit])
    check("draft with no file: still flagged, the earlier attempt's evidence was cleared",
          len(srv.submits) == 1 and not out.resume_seen_on_page
          and "no verified upload of this job's resume" in out.resume_rule_violation,
          f"{srv.submits} {out.resume_rule_violation!r} seen={out.resume_seen_on_page}")

    srv.reset()
    page.goto(f"{base}/workday?file={other}")
    out, _ = drive(cdp, resume, [click_submit])
    check("draft holding another job's file: the Submit click is BLOCKED, nothing sent",
          (st.get("blocked") or {}).get("action") == "block" and "another job" in st["blocked"].get("message", "")
          and not srv.submits, f"{st.get('blocked')} {srv.submits}")


def test_oracle(cdp, srv, page, base, resume, named):
    srv.reset()
    page.goto(base + "/oracle")
    st = {}

    def submit(prompt):
        st["upload"] = prompt
        st["heading"] = page.eval("document.querySelector('h1').innerText")
        return "NEED_CLICK: SUBMIT"

    out, _ = drive(cdp, resume, ["NEED_UPLOAD: resume | id=\"quickProfileImport\"", submit])
    time.sleep(0.5)
    stored = [u for u in srv.uploads if u["path"].endswith("/child/attachments")]
    check("Oracle import: input consumed + attachment stored (201) is UPLOAD_DONE, not 'disappeared'",
          st.get("upload", "").startswith("UPLOAD_DONE") and "stored it as an attachment" in st.get("upload", "")
          and "almost there" in (st.get("heading") or "") and [u["name"] for u in stored] == [named.name],
          f"{st.get('upload', '')[:220]} {srv.uploads}")
    check("Oracle import: SUBMIT then goes through once, with this job's file, no violation",
          len(srv.submits) == 1 and srv.submits[0].get("resume") == named.name and not out.resume_rule_violation,
          f"{srv.submits} {out.resume_rule_violation}")

    # The site only parses the file: no attachment, so no submit.
    srv.reset()
    page.goto(base + "/oracle?parseonly=1")

    def submit2(prompt):
        st["upload2"] = prompt
        return "NEED_CLICK: SUBMIT"

    def note(prompt):
        st["refusal"] = prompt
        return "STATUS: stuck\nDETAIL: resume import stored nothing\nFIELDS_FILLED_JSON: {}\n"

    out, _ = drive(cdp, resume, ["NEED_UPLOAD: resume", submit2, note])
    check("Oracle parse-only: UPLOAD_FAILED says what the site answered (parse, no attachment stored)",
          st.get("upload2", "").startswith("UPLOAD_FAILED") and "ResumeParsers -> HTTP 201" in st.get("upload2", "")
          and "no attachment stored" in st.get("upload2", ""), st.get("upload2", "")[:240])
    check("Oracle parse-only: NEED_CLICK SUBMIT is REFUSED (272's path), nothing sent",
          "Submit refused, resume not verified" in st.get("refusal", "") and not srv.submits,
          f"{st.get('refusal', '')[:200]} {srv.submits}")


def test_plain(cdp, srv, page, base, resume):
    srv.reset()
    page.goto(base + "/plain")
    out, seen = drive(cdp, resume, ["NEED_CLICK: Submit application"])
    time.sleep(0.3)
    check("a form with no resume field still submits through NEED_CLICK",
          len(srv.submits) == 1 and "Submit refused" not in (seen[1] if len(seen) > 1 else ""),
          f"{srv.submits} {seen[1][:160] if len(seen) > 1 else ''}")
    check("...and its audit line says the form showed no resume field",
          "showed no resume field at submit" in out.resume_rule_violation, out.resume_rule_violation)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-extresume-", ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        os.environ["JOBO_UPLOAD_COPY_DIR"] = str(tmp / "copies")
        resume = tmp / "artifacts" / str(JOB_ID) / "resume.pdf"
        resume.parent.mkdir(parents=True)
        resume.write_bytes(b"%PDF-1.4\n% jobo external resume gate regression\n" + os.urandom(64) + b"\n%%EOF\n")
        from module4 import upload_names
        named = Path(upload_names.prepare(JOB_ID, str(resume)))

        guarded("rule", test_rule)
        guarded("audit redaction", test_audit_redaction)
        guarded("stored evidence", test_stored_evidence)
        guarded("prompt", test_prompt)
        guarded("plugin parsing", test_plugin_parsing)

        srv = Server()
        profile = tmp / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,1000", "--user-data-dir=" + str(profile),
             f"--host-resolver-rules=MAP margie.turbohire.co 127.0.0.1:{srv.port}, MAP * ~NOTFOUND, "
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
            base = f"http://margie.turbohire.co:{srv.port}"
            guarded("turbohire upload", lambda: test_turbohire_upload(srv, page, base, named))
            guarded("turbohire gate", lambda: test_turbohire_gate(cdp, srv, page, base, resume, named))
            guarded("plugin veto", lambda: test_plugin_veto(cdp, srv, page, base, named, tmp))
            guarded("resumed workday draft", lambda: test_resumed_workday_draft(cdp, srv, page, base, resume, named))
            guarded("oracle", lambda: test_oracle(cdp, srv, page, base, resume, named))
            guarded("plain", lambda: test_plain(cdp, srv, page, base, resume))
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
