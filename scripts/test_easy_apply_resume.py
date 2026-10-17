#!/usr/bin/env python3
"""LinkedIn Easy Apply must send THIS job's resume (TODO item 18).

2026-10-11: 13 of 27 applications went out through Easy Apply with LinkedIn's
saved, pre-selected resume (the user's old one) and zero uploads; each was sent with
`NEED_CLICK: Submit application`. This suite replays that against
scripts/easy_apply_probe.html, a replica of the real modal (captured read-only
2026-10-12: pre-selected saved cards incl. "resume.pdf", "Upload resume"
button with NO file input in the DOM, radios, Next/Review/"Submit
application"), served by a local server that records what was uploaded and
submitted, under the host www.linkedin.com (mapped to 127.0.0.1) in its own
headless Chromium on a random port. Never 9333.

The driver loop is the real one (module4.agent_driver.run_apply_agent) with
only the model replaced by a scripted fake agent, so the real NEED_UPLOAD and
NEED_CLICK handoffs act on the page. The Hermes plugin's pre_tool_call hook is
called the way Hermes calls it.

Part 1 is offline (names, rule table, prompt, plugin parsing); part 2 drives
the replica. On origin/main the bad path SUBMITS with resume.pdf, so the
browser checks fail there.
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
from module4 import agent_driver, prompts  # noqa: E402

PROBE = Path(__file__).with_name("easy_apply_probe.html")
PLUGIN_DIR = ROOT / "hermes_plugin" / "jobo-browser"
JOB_ID = 1543

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def guarded(name, fn):
    """Run one check group; an exception (e.g. a missing module on an old
    branch) is a FAIL of that group, not a crash of the suite."""
    try:
        fn()
    except Exception as e:
        check(f"{name} (raised {type(e).__name__}: {str(e)[:160]})", False)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- local LinkedIn stand-in ---------------------------------------------------


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
                if path == "/jobs/view/1/":
                    self._send(200, PROBE.read_text())
                elif path == "/ats":
                    self._send(200, "<!doctype html><title>Acme ATS</title><h1>Apply</h1>"
                                    "<form><label>Resume <input type=file id=resume name=resume "
                                    "accept='.pdf'></label><button type=submit>Submit application"
                                    "</button></form>")
                else:
                    self._send(404, "nope")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path.split("?", 1)[0]
                if path == "/api/upload":
                    msg = BytesParser(policy=email_policy).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                    for part in msg.iter_parts():
                        if part.get_filename():
                            data = part.get_payload(decode=True)
                            srv.uploads.append({"name": part.get_filename(),
                                                "sha256": hashlib.sha256(data).hexdigest()})
                            self._send(200, json.dumps({"id": len(srv.uploads)}), "application/json")
                            return
                    self._send(400, "no file")
                elif path == "/api/submit":
                    srv.submits.append(json.loads(body or b"{}"))
                    self._send(200, "{}", "application/json")
                else:
                    self._send(404, "nope")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.uploads.clear()
        self.submits.clear()


class Page:
    """Tiny CDP helper on the browser's one page target."""

    def __init__(self, cdp: str):
        self.cdp = cdp

    def _ws(self):
        from module4 import uploader
        pages = [t for t in json.load(urllib.request.urlopen(self.cdp + "/json/list", timeout=5))
                 if t["type"] == "page"]
        return uploader._Cdp(pages[0]["webSocketDebuggerUrl"])

    def eval(self, expr):
        with self._ws() as c:
            msg = c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return ((msg.get("result") or {}).get("result") or {}).get("value")

    def goto(self, url, settle=0.8):
        with self._ws() as c:
            c.call("Page.enable")
            c.call("Page.navigate", {"url": url})
        for _ in range(50):
            time.sleep(0.1)
            if self.eval("document.readyState") == "complete" and url.split("?")[0] in (self.eval("location.href") or ""):
                break
        time.sleep(settle)

    def open_modal(self):
        self.eval("document.getElementById('easy').click()")
        time.sleep(0.3)

    def selected(self):
        return self.eval("(() => { const r = document.querySelector('#modal fieldset input[type=radio]:checked');"
                         " return r ? r.getAttribute('aria-label') : null; })()")

    def submit_point(self):
        return self.eval("(() => { const b = document.getElementById('submit').getBoundingClientRect();"
                         " return [b.left + b.width/2, b.top + b.height/2]; })()")


# --- fake agent around the REAL driver loop -------------------------------------

VERDICT = "STATUS: submitted\nDETAIL: Easy Apply application submitted.\nFIELDS_FILLED_JSON: {}\n"


def drive(cdp: str, resume: Path, rounds: list, job_id: int = JOB_ID):
    """Run agent_driver.run_apply_agent with scripted agent turns.

    Each round is a string (agent output) or a callable(prompt) -> string, so
    a round can act on the page first (a real agent clicking Next)."""
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


def test_names(resume: Path):
    from module4 import upload_names
    name = upload_names.upload_name(JOB_ID, resume)
    sha = hashlib.sha256(resume.read_bytes()).hexdigest()
    check("upload name is Alex_Example_Resume_<job>_<sha6>.pdf",
          name == f"Alex_Example_Resume_{JOB_ID}_{sha[:6]}.pdf", name)
    copy = Path(upload_names.prepare(JOB_ID, str(resume)))
    check("named copy is byte-identical to module3_resume_pdf_path",
          copy.name == name and copy.read_bytes() == resume.read_bytes() and copy != resume, str(copy))
    check("prepare is idempotent on the named copy", upload_names.prepare(JOB_ID, str(copy)) == str(copy))
    check("another job gets another name", upload_names.upload_name(JOB_ID + 1, resume) != name)
    check("cover letter copy is named Alex_Example_Cover_Letter_<job>_<sha6>.pdf",
          Path(upload_names.prepare(JOB_ID, str(resume), "cover")).name.startswith(f"Alex_Example_Cover_Letter_{JOB_ID}_"))
    check("missing source passes through unchanged", upload_names.prepare(JOB_ID, "/nope/r.pdf") == "/nope/r.pdf")


def test_rule():
    g = _load("rg_offline", PLUGIN_DIR / "resume_guard.py")
    ours = f"Alex_Example_Resume_{JOB_ID}_abc123.pdf"
    card = lambda n, c: {"name": n, "checked": c}  # noqa: E731
    pre = {"modal": True, "cards": [card("resume.pdf", True), card(ours, False)], "selected": ["resume.pdf"]}
    rows = [
        ("pre-selected saved resume.pdf: refuse", pre, False, False),
        ("our card selected: allow", {"modal": True, "cards": [card(ours, True)], "selected": [ours]}, False, True),
        ("no card selected: refuse", {"modal": True, "cards": [card(ours, False)], "selected": []}, True, False),
        ("review step shows our file: allow", {"modal": True, "shown_docs": [ours]}, False, True),
        ("review step shows resume.pdf: refuse", {"modal": True, "shown_docs": ["resume.pdf"]}, True, False),
        ("review shows our cover letter only, verified: allow",
         {"modal": True, "shown_docs": [f"Alex_Example_Cover_Letter_{JOB_ID}_ffffff.pdf"]}, True, True),
        ("no resume on step, never verified: refuse", {"modal": True}, False, False),
        ("no resume on step, verified earlier: allow", {"modal": True}, True, True),
        ("no Easy Apply modal: allow", {"modal": False}, False, True),
        ("a saved card named like ours but another job: refuse",
         {"modal": True, "cards": [card(f"Alex_Example_Resume_{JOB_ID + 7}_abc123.pdf", True)],
          "selected": [f"Alex_Example_Resume_{JOB_ID + 7}_abc123.pdf"]}, True, False),
    ]
    for label, state, verified, want in rows:
        ok, reason = g.judge(state, ours, verified)
        check(f"rule: {label}", ok is want, reason)


def test_prompt(resume: Path):
    sp = prompts.SYSTEM_PROMPT
    check("prompt: Easy Apply resume step always asks NEED_UPLOAD: resume",
          "LinkedIn Easy Apply resume step" in sp and "NEED_UPLOAD: resume" in sp and "pre-selects one" in sp)
    check("prompt: never click another card / never deselect ours",
          "never click another resume card and never deselect ours" in sp)
    check("prompt: generic rule for every ATS (saved resume / resume from profile)",
          "use resume from profile" in sp and "previously saved resume" in sp)
    from module4 import upload_names
    named = upload_names.prepare(JOB_ID, str(resume))
    up = prompts.user_prompt(job_id=JOB_ID, company="Acme", title="Dev", url="u", resume_path=named, cover_path=None)
    check("task prompt names this job's upload file", f"resume_upload_name: {Path(named).name}" in up, up[:300])
    check("no em dash in the prompt", "\u2014" not in sp.replace("No em dashes (\u2014), ever", ""))


def test_plugin_parsing():
    plugin = _load("jobo_browser_plugin_resume_t", PLUGIN_DIR / "__init__.py")
    rg = plugin.resume_guard
    plugin.account_guard.remember_refs('- button "Submit application" [ref=e9]\n- button "Next" [ref=e10]')
    gated = lambda t, a: rg._submit_attempt(t, a, plugin._ref_name)[0]  # noqa: E731
    check("plugin: browser_click on the Submit ref is gated", gated("browser_click", {"ref": "@e9"}))
    check("plugin: browser_click on Next is not gated", not gated("browser_click", {"ref": "e10"}))
    check("plugin: a console script clicking Submit is gated",
          gated("browser_console", {"expression": "[...document.querySelectorAll('button')].find(b=>/Submit/.test(b.innerText)).click()"}))
    check("plugin: a read-only console script is not gated",
          not gated("browser_console", {"expression": "document.title"}))
    old = os.environ.pop("JOBO_UPLOAD_RESUME_NAME", None)
    try:
        check("plugin: outside a jobo apply run the guard does nothing",
              rg.check_tool_call("browser_click", {"ref": "e9"}, plugin._ref_name) is None)
    finally:
        if old is not None:
            os.environ["JOBO_UPLOAD_RESUME_NAME"] = old


# --- part 2: the replica in a real browser ---------------------------------------


def test_bad_path_single_step(cdp, srv, page, base, resume):
    """The 2026-10-11 shape: one page, saved resume.pdf pre-selected."""
    srv.reset()
    page.goto(base + "/jobs/view/1/?steps=1")
    page.open_modal()
    check("replica: resume.pdf is pre-selected and the DOM has no file input",
          page.selected() == "resume.pdf" and page.eval("document.querySelectorAll('input[type=file]').length") == 0)
    names = {}

    def after_refusal(prompt):
        names["refusal"] = prompt
        names["sel_after_refusal"] = page.selected()
        return "NEED_UPLOAD: resume"

    def after_upload(prompt):
        names["upload_reply"] = prompt
        names["sel_after_upload"] = page.selected()
        return "NEED_CLICK: Submit application"

    out, seen = drive(cdp, resume, ["NEED_CLICK: Submit application", after_refusal, after_upload])
    from module4 import upload_names
    want = upload_names.upload_name(JOB_ID, resume)
    sha = hashlib.sha256(resume.read_bytes()).hexdigest()
    first = names.get("refusal", "")
    check("bad path: NEED_CLICK Submit with the saved resume.pdf selected is REFUSED (no click sent)",
          first.startswith("CLICK_FAILED") and "Easy Apply Submit refused" in first
          and "resume.pdf" in first and names.get("sel_after_refusal") == "resume.pdf",
          first[:200] or f"submits={srv.submits}")
    check("refusal tells the agent to NEED_UPLOAD: resume", "NEED_UPLOAD: resume" in first)
    check("upload: the server received exactly one file, named for this job",
          [u["name"] for u in srv.uploads] == [want], str(srv.uploads))
    check("upload: the uploaded bytes are the job's own resume (sha256 match)",
          bool(srv.uploads) and srv.uploads[0]["sha256"] == sha)
    check("upload: our new card is the selected one and the agent is told so",
          names.get("sel_after_upload") == want and names.get("upload_reply", "").startswith("UPLOAD_DONE")
          and want in names.get("upload_reply", ""), names.get("upload_reply", "")[:200])
    check("submit: exactly one application sent, carrying our uploaded file",
          len(srv.submits) == 1 and srv.submits[0].get("resume") == want and srv.submits[0].get("upload_id"),
          str(srv.submits))
    check("outcome records the verified Easy Apply upload, no rule violation",
          out.status == "submitted" and out.easy_apply_resume_verified and out.resume_uploads_ok == 1
          and not out.resume_rule_violation, f"{out}")


def test_never_uploads(cdp, srv, page, base, resume):
    srv.reset()
    page.goto(base + "/jobs/view/1/?steps=1")
    page.open_modal()
    out, seen = drive(cdp, resume, ["NEED_CLICK: Submit application", "NEED_CLICK", "NEED_CLICK: Submit application",
                                    "NEED_CLICK: Submit application", "NEED_CLICK: Submit application"])
    refused = [p for p in seen[1:] if "Easy Apply Submit refused" in p]
    check("an agent that never uploads can NEVER submit (5 attempts incl. a blank NEED_CLICK)",
          not srv.submits and len(refused) >= 4, f"submits={srv.submits} refused={len(refused)}")
    check("its claimed 'submitted' verdict is flagged as a resume-rule violation",
          bool(out.resume_rule_violation) and "Easy Apply" in out.resume_rule_violation, out.resume_rule_violation)


def test_switch_back(cdp, srv, page, base, resume):
    """Upload does not auto-select; later the agent clicks an old card."""
    srv.reset()
    page.goto(base + "/jobs/view/1/?steps=1&autoselect=0")
    page.open_modal()
    from module4 import upload_names
    want = upload_names.upload_name(JOB_ID, resume)
    st = {}

    def pick_old(prompt):
        st["after_upload"] = page.selected()
        page.eval("document.querySelectorAll('#modal fieldset input[type=radio]')[1].click()")  # an old card
        st["picked"] = page.selected()
        return "NEED_CLICK: Submit application"

    def reupload(prompt):
        st["refusal"] = prompt
        return "NEED_UPLOAD: resume"

    def submit(prompt):
        st["reupload"] = prompt
        st["after_reupload"] = page.selected()
        return "NEED_CLICK: Submit application"

    out, seen = drive(cdp, resume, ["NEED_UPLOAD: resume", pick_old, reupload, submit])
    check("upload that LinkedIn does not auto-select: the driver selects our card itself",
          st.get("after_upload") == want, str(st.get("after_upload")))
    check("agent switches to an old saved card: Submit refused again",
          st.get("picked") != want and "Easy Apply Submit refused" in st.get("refusal", ""), st.get("refusal", "")[:160])
    check("NEED_UPLOAD again re-selects our card without a second upload",
          st.get("after_reupload") == want and len(srv.uploads) == 1 and "ALREADY_ATTACHED" in st.get("reupload", ""),
          f"{st.get('reupload', '')[:120]} uploads={len(srv.uploads)}")
    check("then exactly one submission, with our file",
          len(srv.submits) == 1 and srv.submits[0].get("resume") == want, str(srv.submits))


def test_multi_step(cdp, srv, page, base, resume):
    from module4 import upload_names
    want = upload_names.upload_name(JOB_ID, resume)
    # Agent skips the resume step entirely, reaches Review.
    srv.reset()
    page.goto(base + "/jobs/view/1/?steps=4")
    page.open_modal()

    def to_review(prompt):
        for _ in range(3):
            page.eval("[...document.querySelectorAll('#modal footer button')].find(b => !b.classList.contains('hidden') && /Next|Review/.test(b.innerText)).click()")
            time.sleep(0.1)
        return "NEED_CLICK: Submit application"

    st = {}

    def note(prompt):
        st["refusal"] = prompt
        return "STATUS: stuck\nDETAIL: refused\nFIELDS_FILLED_JSON: {}\n"

    out, _ = drive(cdp, resume, [to_review, note])
    review = page.eval("document.getElementById('review').innerText")
    check("multi-step: Review shows resume.pdf (resume step skipped) -> Submit refused",
          "resume.pdf" in (review or "") and "Easy Apply Submit refused" in st.get("refusal", "") and not srv.submits,
          f"{review!r} {st.get('refusal', '')[:120]}")

    # Proper flow: Next to the resume step, upload, Next, Review, Submit.
    srv.reset()
    page.goto(base + "/jobs/view/1/?steps=4")
    page.open_modal()

    def to_resume(prompt):
        page.eval("document.getElementById('next').click()")
        return "NEED_UPLOAD: resume"

    def forward(prompt):
        st["upload"] = prompt
        page.eval("document.getElementById('next').click()")
        page.eval("document.getElementById('reviewbtn').click()")
        st["review"] = page.eval("document.getElementById('review').innerText")
        return "NEED_CLICK: Submit application"

    out, _ = drive(cdp, resume, [to_resume, forward])
    check("multi-step: upload on the resume step, Review shows our file, one submission with it",
          st.get("upload", "").startswith("UPLOAD_DONE") and want in (st.get("review") or "")
          and len(srv.submits) == 1 and srv.submits[0].get("resume") == want,
          f"{st.get('upload', '')[:100]} {st.get('review')!r} {srv.submits}")


def test_plugin_veto(cdp, srv, page, base, resume, tmp: Path):
    from module4 import upload_names
    named = Path(upload_names.prepare(JOB_ID, str(resume)))
    plugin = _load("jobo_browser_plugin_resume_live", PLUGIN_DIR / "__init__.py")
    state = tmp / "guard_state.json"
    state.write_text(json.dumps({"verified": False}))
    env = {"BROWSER_CDP_URL": cdp, "JOBO_UPLOAD_RESUME_NAME": named.name, "JOBO_RESUME_GUARD_FILE": str(state)}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        srv.reset()
        page.goto(base + "/jobs/view/1/?steps=1")
        page.open_modal()
        plugin.account_guard.remember_refs('- button "Submit application" [ref=e9]\n- button "Upload resume" [ref=e8]')
        x, y = page.submit_point()
        blocked = plugin._pre_tool_call("browser_click", {"ref": "@e9"})
        check("plugin veto: browser_click Submit with saved resume.pdf selected is BLOCKED",
              (blocked or {}).get("action") == "block" and "resume.pdf" in blocked.get("message", ""), str(blocked)[:200])
        cdpm = plugin._pre_tool_call("browser_cdp", {"method": "Input.dispatchMouseEvent",
                                                     "params": {"type": "mousePressed", "x": x, "y": y}})
        check("plugin veto: a raw CDP mouse press on Submit is BLOCKED", (cdpm or {}).get("action") == "block")
        js = plugin._pre_tool_call("browser_console", {"expression": "document.getElementById('submit').click()"})
        check("plugin veto: a console script clicking Submit is BLOCKED", (js or {}).get("action") == "block")
        check("plugin veto: clicking 'Upload resume' is never blocked",
              plugin._pre_tool_call("browser_click", {"ref": "e8"}) is None)
        check("plugin veto: a mouse press elsewhere is not blocked",
              plugin._pre_tool_call("browser_cdp", {"method": "Input.dispatchMouseEvent",
                                                    "params": {"type": "mousePressed", "x": 5, "y": 5}}) is None)
        # Now upload ours through the driver path and re-check.
        from module4 import easy_apply
        res = easy_apply.upload_and_select(cdp, str(named))
        check("easy_apply.upload_and_select attaches the uniquely named file and selects its card",
              res is not None and res.ok and page.selected() == named.name, res and res.detail)
        check("plugin veto lifts once our card is selected",
              plugin._pre_tool_call("browser_click", {"ref": "e9"}) is None)
        check("nothing was submitted by any guard check", not srv.submits, str(srv.submits))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_other_sites(cdp, srv, page, base, resume):
    from module4 import upload_names
    want = upload_names.upload_name(JOB_ID, resume)
    # Same modal markup but NOT on linkedin.com: the gate stays out of the way.
    srv.reset()
    page.goto(f"http://127.0.0.1:{srv.port}/jobs/view/1/?steps=1")
    page.open_modal()
    out, seen = drive(cdp, resume, ["NEED_CLICK: Submit application"])
    check("non-LinkedIn site: the Easy Apply gate does not interfere with Submit",
          len(srv.submits) == 1 and "Easy Apply Submit refused" not in (seen[1] if len(seen) > 1 else ""),
          f"{srv.submits} {seen[1][:120] if len(seen) > 1 else ''}")
    # Generic ATS input: the uniquely named copy is what lands in the input.
    page.goto(f"http://127.0.0.1:{srv.port}/ats")
    st = {}

    def note(prompt):
        st["reply"] = prompt
        return VERDICT

    drive(cdp, resume, ["NEED_UPLOAD: resume", note])
    got = page.eval("document.getElementById('resume').files[0] && document.getElementById('resume').files[0].name")
    check("generic ATS: NEED_UPLOAD attaches the job-named copy (file chip shows the job id)",
          got == want and st.get("reply", "").startswith("UPLOAD_DONE"), f"{got} {st.get('reply', '')[:120]}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-easyapply-", ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        os.environ["JOBO_UPLOAD_COPY_DIR"] = str(tmp / "copies")
        resume = tmp / "artifacts" / str(JOB_ID) / "resume.pdf"
        resume.parent.mkdir(parents=True)
        resume.write_bytes(b"%PDF-1.4\n% jobo easy apply regression resume\n" + os.urandom(64) + b"\n%%EOF\n")

        guarded("names", lambda: test_names(resume))
        guarded("rule", test_rule)
        guarded("prompt", lambda: test_prompt(resume))
        guarded("plugin parsing", test_plugin_parsing)

        srv = Server()
        profile = tmp / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,1000", "--user-data-dir=" + str(profile),
             f"--host-resolver-rules=MAP www.linkedin.com 127.0.0.1:{srv.port}, MAP * ~NOTFOUND, "
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
            base = f"http://www.linkedin.com:{srv.port}"
            page.goto(base + "/jobs/view/1/?steps=1")
            check("replica is served under www.linkedin.com", page.eval("location.hostname") == "www.linkedin.com")
            guarded("bad path", lambda: test_bad_path_single_step(cdp, srv, page, base, resume))
            guarded("never uploads", lambda: test_never_uploads(cdp, srv, page, base, resume))
            guarded("switch back", lambda: test_switch_back(cdp, srv, page, base, resume))
            guarded("multi step", lambda: test_multi_step(cdp, srv, page, base, resume))
            guarded("plugin veto", lambda: test_plugin_veto(cdp, srv, page, base, resume, tmp))
            guarded("other sites", lambda: test_other_sites(cdp, srv, page, base, resume))
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
