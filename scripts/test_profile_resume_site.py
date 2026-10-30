#!/usr/bin/env python3
"""hirist.tech applies with the profile's resume: Apply only after ours is in it (GHO-106).

Run 2026-10-30 22:00 IST, job 3294: LinkedIn "Apply on company website" led
to a hirist.tech job page. The agent clicked "Apply", which opened hirist's
login popup; it asked NEED_OTP, typed the code, and hirist sent the
application at once (redirect to /job/applied) with whatever resume the
candidate's hirist profile held. No upload step ever existed, the page had no
resume field, so the resume gate let "Apply" through and only the after-the-
fact RESUME RULE VIOLATION line noticed.

hirist's own bundle (js-static.hirist.tech, read 2026-10-30): the apply call
is POST /apply?jobcode=<id> with no file; the profile's resume is replaced by
POST /files (multipart, mediaType=TextResume) the moment a file is picked.
So the job's resume can be put in the profile first, then Apply sends it.

The replica below has that shape: a job page whose Apply opens the login
popup and applies on the sixth code digit, and a profile page whose file
input replaces the profile resume. It runs on a local server mapped to
www.hirist.tech in a headless Chromium on a random port (never 9333). The
driver loop is the real one with a scripted fake agent; the Hermes plugin's
pre_tool_call hook is called the way Hermes calls it.

On origin/main the Apply click goes through and the server records an
application carrying the old profile resume.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from module4 import agent_driver, prompts, run_report  # noqa: E402
from test_external_resume_gate import PLUGIN_DIR, VERDICT, Page, _load, drive  # noqa: E402

JOB_ID = 3294
OLD_RESUME = "resume_2025.pdf"

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def guarded(name, fn):
    try:
        fn()
    except Exception as e:
        check(f"{name} (raised {type(e).__name__}: {str(e)[:160]})", False)


# --- replica -------------------------------------------------------------------------

# Login popup shared by the home and job pages: email, Get OTP, six digit
# boxes. The sixth digit verifies; `onVerified` decides what happens next.
LOGIN = r"""
<div role="dialog" id="login" hidden><p>Login here</p>
  <input id="email" aria-label="Email or Mobile Number"><button type="button" id="getotp">Get OTP</button>
  <div id="digits" hidden>""" + "".join(f'<input class="d" maxlength="1" id="d{i}">' for i in range(6)) + r"""</div>
</div>
<script>
document.getElementById('getotp').onclick = () => { document.getElementById('digits').hidden = false; };
document.getElementById('d5').addEventListener('input', () => {
  document.cookie = 'hl=1; path=/'; document.getElementById('login').hidden = true; onVerified(); });
const loggedIn = () => document.cookie.includes('hl=1');
</script>"""

HOME = r"""<!doctype html><title>hirist.tech - Tech Jobs</title>
<header><a href="/">hirist</a> <button type="button" id="loginbtn">Login</button></header>
<main><h1>Find tech jobs</h1></main>""" + LOGIN + r"""
<script>
document.getElementById('loginbtn').onclick = () => { document.getElementById('login').hidden = false; };
function onVerified() { location.href = '/jobfeed'; }
</script>"""

# The job page: Apply when signed out opens the popup and applies once the
# code is in (3294's path); signed in, Apply applies at once. Either way the
# application carries the profile's resume, never a file of its own.
JOB = r"""<!doctype html><title>Acme - AI Full Stack Developer | hirist.tech</title>
<header><a href="/">hirist</a> <button type="button">Login</button></header>
<main><h1>Acme - AI Full Stack Developer</h1><p>Gurgaon/Gurugram</p>
  <button type="button" id="apply">Apply</button> <button type="button">Save</button></main>""" + LOGIN + r"""
<script>
async function apply() {
  await fetch('/apply?jobcode=1680134', {method: 'POST', body: '{}'});
  location.href = '/job/applied?jobId=1680134';
}
function onVerified() { apply(); }
document.getElementById('apply').onclick = () => {
  if (loggedIn()) apply(); else document.getElementById('login').hidden = false; };
</script>"""

# My Profile's resume section: picking a file replaces the profile resume
# at once (POST /files, mediaType=TextResume) and shows its name.
PROFILE = r"""<!doctype html><title>My Profile | hirist.tech</title>
<main><h1>My Profile</h1>
<section id="resume"><h3>Resume</h3><p id="current"></p>
  <div><span>Upload Resume</span> <input type="file" id="cv" accept=".pdf,.doc,.docx"></div>
  <p id="toast" role="alert"></p></section></main>
<script>
fetch('/profile').then(r => r.json()).then(p => {
  document.getElementById('current').textContent = p.resume + ' (Uploaded On: 12 Mar 2025)'; });
document.getElementById('cv').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f, f.name); fd.append('mediaType', 'TextResume');
  const r = await fetch('/files', {method: 'POST', body: fd});
  if (r.ok) {
    document.getElementById('current').textContent = f.name + ' (Uploaded On: today)';
    document.getElementById('toast').textContent = 'Your resume has been uploaded to your profile successfully!';
  }
});
</script>"""


class Server:
    def __init__(self):
        self.profile_resume = OLD_RESUME
        self.applications: list[dict] = []
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
                if path == "/profile":
                    self._send(200, json.dumps({"resume": srv.profile_resume}), "application/json")
                    return
                page = {"/": HOME, "/jobfeed": HOME, "/myprofile": PROFILE,
                        "/j/acme-ai-full-stack-developer-1680134": JOB}.get(path)
                if path == "/job/applied":
                    page = "<!doctype html><title>Applied</title><p>Similar Jobs That You Might Be Interested In</p>"
                self._send(200, page) if page else self._send(404, "nope")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path.split("?", 1)[0]
                if path == "/apply":
                    srv.applications.append({"resume": srv.profile_resume})
                    self._send(200, "{}", "application/json")
                    return
                if path == "/files":
                    marker = b'filename="'
                    i = body.find(marker)
                    if i >= 0:
                        srv.profile_resume = body[i + len(marker):body.index(b'"', i + len(marker))].decode()
                        self._send(200, json.dumps({"url": "/media/x.pdf"}), "application/json")
                        return
                self._send(400, "no file")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.profile_resume = OLD_RESUME
        self.applications.clear()


# --- part 1: offline -----------------------------------------------------------------


def test_rule():
    g = _load("rg_profile_offline", PLUGIN_DIR / "resume_guard.py")
    ours = f"Alex_Example_Resume_{JOB_ID}_abc123.pdf"
    job = "https://www.hirist.tech/j/acme-ai-full-stack-developer-1680134?ref=linkedin"
    check("site: www.hirist.tech and hirist.com are profile-resume sites, a lookalike is not",
          g.profile_resume_site(job) == "hirist.tech" and g.profile_resume_site("https://hirist.com/x") == "hirist.com"
          and g.profile_resume_site("https://nothirist.tech/j/1") == "" and g.profile_resume_site("") == "")
    page = {"external": True, "url": job, "slots": []}
    rows = [
        ("hirist job page, no resume field, nothing uploaded: refuse (3294)", [page], {}, False),
        ("hirist page that could not be read: still refused by its URL",
         [{"external": True, "url": job, "unread": True}], {}, False),
        ("hirist after this job's resume upload was verified: allow", [page], {"resume_uploads_ok": 1}, True),
        ("hirist profile tab shows this job's file: allow",
         [page, {"external": True, "url": "https://www.hirist.tech/myprofile", "shown_ours": True}], {}, True),
        ("any other site with no resume field: still allowed",
         [{"external": True, "url": "https://jobs.example.com/apply", "slots": []}], {}, True),
    ]
    for label, states, gs, want in rows:
        ok, reason = g.judge_external(states, ours, gs)
        check(f"rule: {label}", ok is want, reason)
    ok, reason = g.judge_external([page], ours, {})
    msg = g.refusal_message_external(reason, ours)
    check("refusal: log in from the home page, NEED_UPLOAD to the profile, else stuck upload:profile_only",
          "Apply refused" in msg and "home page" in msg and "NEED_OTP: hirist.tech" in msg
          and "NEED_UPLOAD: resume" in msg and "upload:profile_only hirist.tech" in msg, msg)
    other = g.refusal_message_external("this form has a resume field ('x') and ...", ours)
    check("refusal on other sites is unchanged", "Submit refused, resume not verified" in other
          and "profile_only" not in other, other)


def test_report_and_prompt():
    sig = run_report.signature_for("stuck: upload:profile_only hirist.tech - UPLOAD_FAILED: no file input")
    check("run report: the stuck detail has its own signature", sig == "upload:profile_only", sig)
    sp = prompts.SYSTEM_PROMPT
    note = sp.split("**hirist.tech", 1)[1].split("\n- **", 1)[0] if "**hirist.tech" in sp else ""
    check("prompt: hirist note says never click Apply first, log in from home, upload to the profile",
          "never click Apply first" in note and "https://www.hirist.tech/" in note
          and "NEED_UPLOAD: resume" in note and "upload:profile_only" in note, note[:200])


# --- part 2: replica in a real browser -----------------------------------------------


def _env(cdp: str, named: Path, state: Path) -> dict:
    return {"BROWSER_CDP_URL": cdp, "JOBO_UPLOAD_RESUME_NAME": named.name, "JOBO_RESUME_GUARD_FILE": str(state)}


def test_plugin_veto(cdp, srv, page, base, named, tmp: Path):
    plugin = _load("jobo_browser_plugin_profile", PLUGIN_DIR / "__init__.py")
    state = tmp / "guard_state.json"
    env = _env(cdp, named, state)
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        srv.reset()
        page.goto(base + "/j/acme-ai-full-stack-developer-1680134?utm_source=LinkedIn")
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 0, "resume_upload_failed": ""}))
        plugin.account_guard.remember_refs('- button "right Login" [ref=e23]\n- button "Apply" [ref=e38]')
        blocked = plugin._pre_tool_call("browser_click", {"ref": "@e38"})
        check("plugin veto: browser_click on hirist's 'Apply' before our resume is in the profile is BLOCKED",
              (blocked or {}).get("action") == "block" and "profile" in blocked.get("message", ""),
              str(blocked)[:200])
        js = plugin._pre_tool_call("browser_console", {"expression": "document.getElementById('apply').click()"})
        check("plugin veto: a console script clicking it is BLOCKED too", (js or {}).get("action") == "block")
        check("plugin veto: the header Login button is not held up",
              plugin._pre_tool_call("browser_click", {"ref": "e23"}) is None)
        state.write_text(json.dumps({"verified": False, "resume_uploads_ok": 1, "resume_upload_failed": ""}))
        check("plugin veto lifts once the driver verified this job's resume upload",
              plugin._pre_tool_call("browser_click", {"ref": "e38"}) is None)
        check("nothing was applied by any guard check", not srv.applications, str(srv.applications))
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_3294_path(cdp, srv, page, base, resume, named):
    """The agent clicks Apply first; without the veto the code sends the old resume."""
    plugin = _load("jobo_browser_plugin_3294", PLUGIN_DIR / "__init__.py")
    st: dict = {}

    def click_apply(prompt):
        env = {**agent_driver._JOB.env(), "BROWSER_CDP_URL": cdp}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            plugin.account_guard.remember_refs('- button "Apply" [ref=e38]')
            st["blocked"] = plugin._pre_tool_call("browser_click", {"ref": "e38"})
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        if st["blocked"]:
            return "NEED_CLICK: Apply"
        # What 3294's agent did: Apply, then the emailed code in the popup.
        page.eval("document.getElementById('apply').click()")
        page.eval("document.getElementById('getotp').click()")
        page.eval("for (let i = 0; i < 6; i++) { const d = document.getElementById('d' + i); d.value = '8';"
                  " d.dispatchEvent(new Event('input', {bubbles: true})); }")
        time.sleep(0.6)
        return VERDICT

    def note(prompt):
        st["refusal"] = prompt
        return "STATUS: stuck\nDETAIL: upload:profile_only hirist.tech - not tried\nFIELDS_FILLED_JSON: {}\n"

    srv.reset()
    page.goto(base + "/j/acme-ai-full-stack-developer-1680134?utm_source=LinkedIn")
    out, _ = drive(cdp, resume, [click_apply, note], job_id=JOB_ID)
    check("3294's path: the direct Apply click is BLOCKED and nothing is sent with the old profile resume",
          (st.get("blocked") or {}).get("action") == "block" and not srv.applications,
          f"blocked={str(st.get('blocked'))[:120]} applications={srv.applications}")
    check("3294's path: NEED_CLICK Apply is refused by the driver too, with the profile-first steps",
          "Apply refused" in st.get("refusal", "") and "NEED_UPLOAD: resume" in st.get("refusal", "")
          and not srv.applications, st.get("refusal", "")[:200])
    check("3294's path: the job ends stuck, not submitted", out.status == "stuck", out.status)


def test_profile_first(cdp, srv, page, base, resume, named):
    """The prompt's path: log in from home, upload to the profile, then Apply."""
    st: dict = {}

    def login_and_open_profile(prompt):
        page.goto(base + "/")
        page.eval("document.getElementById('loginbtn').click(); document.getElementById('getotp').click();"
                  "for (let i = 0; i < 6; i++) { const d = document.getElementById('d' + i); d.value = '8';"
                  " d.dispatchEvent(new Event('input', {bubbles: true})); }")
        time.sleep(0.4)
        st["after_login"] = list(srv.applications)
        page.goto(base + "/myprofile")
        return "NEED_UPLOAD: resume"

    def back_to_job(prompt):
        st["upload"] = prompt
        page.goto(base + "/j/acme-ai-full-stack-developer-1680134")
        return "NEED_CLICK: Apply"

    srv.reset()
    page.goto(base + "/j/acme-ai-full-stack-developer-1680134")
    out, seen = drive(cdp, resume, [login_and_open_profile, back_to_job], job_id=JOB_ID)
    time.sleep(0.6)
    check("profile first: logging in from the home page applies to nothing",
          st.get("after_login") == [], str(st.get("after_login")))
    check("profile first: NEED_UPLOAD on the profile page is UPLOAD_DONE and replaces the profile resume",
          st.get("upload", "").startswith("UPLOAD_DONE") and srv.profile_resume == named.name,
          f"{st.get('upload', '')[:200]} profile={srv.profile_resume}")
    check("profile first: Apply then goes through once, carrying this job's resume, no violation",
          [a["resume"] for a in srv.applications] == [named.name] and out.status == "submitted"
          and not out.resume_rule_violation,
          f"{srv.applications} {out.status} {out.resume_rule_violation!r} {seen[-1][:160] if seen else ''}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-profileresume-", ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        os.environ["JOBO_UPLOAD_COPY_DIR"] = str(tmp / "copies")
        resume = tmp / "artifacts" / str(JOB_ID) / "resume.pdf"
        resume.parent.mkdir(parents=True)
        resume.write_bytes(b"%PDF-1.4\n% jobo profile resume regression\n" + os.urandom(64) + b"\n%%EOF\n")
        from module4 import upload_names
        named = Path(upload_names.prepare(JOB_ID, str(resume)))

        guarded("rule", test_rule)
        guarded("report and prompt", test_report_and_prompt)

        srv = Server()
        profile = tmp / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,1000", "--user-data-dir=" + str(profile),
             f"--host-resolver-rules=MAP www.hirist.tech 127.0.0.1:{srv.port}, MAP * ~NOTFOUND, "
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
            base = f"http://www.hirist.tech:{srv.port}"
            guarded("plugin veto", lambda: test_plugin_veto(cdp, srv, page, base, named, tmp))
            guarded("3294 path", lambda: test_3294_path(cdp, srv, page, base, resume, named))
            guarded("profile first", lambda: test_profile_first(cdp, srv, page, base, resume, named))
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
