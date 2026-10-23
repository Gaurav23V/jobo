#!/usr/bin/env python3
"""Three upload shapes from the 2026-10-23 22:00 run (GHO-85).

* Southridge 2297: four role panels, one byte-identical `input.cv-input` each
  under the same "Apply" heading; only the form id (`#engineer-form`) tells
  them apart. Every hint the agent gave, the CSS selector
  `#engineer-form .cv-input` included, was refused as "ambiguous upload
  slots". Page: scripts/upload_role_panels_probe.html.
* Hirebridge 2396 (Humongous, ASP.NET QuickApply): the input's onchange
  clicks a hidden parse button whose __doPostBack POSTs the whole form, file
  included, and the server answers with a fresh copy of the page. The uploader
  refused the new page as "original frame/document/form replaced; refusing
  scope drift" four times. Page: served below by a local postback server.
* Munson 2623 (Eightfold): the upload waits on a "Data Privacy Notice"
  (Close | Cancel | I understand); only agree/accept were clicked. Page:
  scripts/upload_company_sites_probe.html?site=eightfold&notice=1.

Self-isolated: own headless Chromium on a random port (never 9333).
Run: bash scripts/run_regression_tests.sh test_upload_panels_postback
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import uploader  # noqa: E402
from scripts.test_upload_stability import Stability  # noqa: E402

PANELS = Path(__file__).resolve().parent / "upload_role_panels_probe.html"
SITES = Path(__file__).resolve().parent / "upload_company_sites_probe.html"
CDP = ""
RESUME: Path
results: list[tuple[str, bool, str]] = []

# --- Hirebridge-style postback server ---------------------------------------
# GET  /v3/ApplicationV2/QuickApply.aspx?echo=<visible|markup|none>
# POST the same address: the form, file included; answers with the page again.
#   echo=visible  the new page shows the file name in #resumeName
#   echo=markup   only `var ResumeUploadFilename = '<name>'` carries it
#   echo=none     the server drops the file (nothing carries the name)
#   move=1        the post-back answers from another address (a real change of page)
POSTS: list[str] = []
INPUT_NAME = "ctl00$pageContent$ResumeUpload1$uploadedresume"


def _page(echo: str, name: str) -> str:
    shown = name if echo == "visible" else ""
    script_name = name if echo in ("visible", "markup") else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>QuickApply</title></head><body>
<form name="aspnetForm" method="post" id="aspnetForm" enctype="multipart/form-data">
  <input name="ctl00$pageContent$Email" type="text" value="">
  <input type="button" name="ctl00$pageContent$btnParseResume" value="hidden parse" id="btnParseResume"
         onclick="document.getElementById('aspnetForm').submit()" style="visibility: hidden; height:0px" />
  <h4>Upload Your Resume / CV <span>(Required)</span></h4>
  <a onclick="document.getElementById('uploadedresume').click()" id="btnMyComputer">My Computer</a>
  <div style="height: 0px;width: 0px; overflow:hidden;">
    <input name="{INPUT_NAME}" type="file" id="uploadedresume" onchange="sub(this)"
           accept="application/pdf,application/msword,text/plain" />
  </div>
  <div id="resumeName" class="resume-filename">{shown}</div>
  <input type="submit" name="ctl00$pageContent$btnNext" value="Submit">
</form>
<script>
  var ResumeUploadFilename = '{script_name}';
  function sub(obj) {{ document.getElementById('btnParseResume').click(); }}
  window.__state = () => JSON.stringify({{name: ResumeUploadFilename}});
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: str) -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v3/Thanks.aspx"):
            return self._send("<html><body><input type=file name='other'><p>Thanks</p></body></html>")
        q = dict(re.findall(r"(\w+)=(\w+)", self.path.split("?", 1)[-1]))
        self._send(_page(q.get("echo", "visible"), ""))

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        m = re.search(rb'name="' + re.escape(INPUT_NAME.encode()) + rb'"; filename="([^"]*)"', body)
        name = m.group(1).decode() if m else ""
        POSTS.append(name)
        time.sleep(0.5)  # the server parses the resume before answering
        q = dict(re.findall(r"(\w+)=(\w+)", self.path.split("?", 1)[-1]))
        if q.get("move") == "1":
            self.send_response(303)
            self.send_header("Location", "/v3/Thanks.aspx")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        echo = q.get("echo", "visible")
        self._send(_page(echo, "" if echo == "none" else name))


# --- helpers -----------------------------------------------------------------

def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:66s} {note}")


def open_tab(url: str) -> dict:
    # Quoted: DevTools reads an unquoted `&` as the end of the URL.
    req = urllib.request.Request(f"{CDP}/json/new?{urllib.parse.quote(url, safe=':/')}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.load(r)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if state(tab) is not None:
                break
        except Exception:
            pass
        time.sleep(0.05)
    return tab


def close_tab(tab: dict) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab['id']}", timeout=5).read()
    except Exception:
        pass


def state(tab: dict):
    with uploader._Cdp(tab["webSocketDebuggerUrl"]) as c:
        r = c.call("Runtime.evaluate", {"expression": "window.__state && window.__state()", "returnByValue": True})
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else None


def run(url: str, hint: str = "", settle: float = 0.3):
    tab = open_tab(url)
    try:
        res = uploader.upload_file(CDP, str(RESUME), want="resume", hint=hint)
        time.sleep(settle)
        return res, state(tab)
    finally:
        close_tab(tab)


# --- checks ------------------------------------------------------------------

def _southridge() -> None:
    print("Southridge 2297: four role panels, identical inputs, only the form id differs")
    # The first four are the 22:00 run's hints; the last two the live retry's
    # (selectors inside prose), which were still refused before this pass.
    for hint in ("#engineer-form .cv-input", "engineer-form", "inside engineer-form",
                 "form#engineer-form input[type=file]", "inside #engineer-form .cv-input",
                 'under "Apply" in form#engineer-form'):
        for open_ in ("", "engineer"):
            res, st = run(PANELS.as_uri() + (f"?open={open_}" if open_ else ""), hint)
            # In a closed panel nothing visible changes, so the uploader re-sends
            # the change event once (the Greenhouse rule): one write, <=2 events.
            check(f"hint {hint[:30]!r} ({'panel open' if open_ else 'all closed'}) -> engineer form only",
                  res.ok and set(st["uploads"]) == {"engineer"} and st["files"]
                  == {"apply": 0, "recruiter": 0, "engineer": 1, "creator": 0},
                  f"ok={res.ok} uploads={st['uploads']} {res.detail[:70]}")
    res, st = run(PANELS.as_uri(), "#recruiter-form .cv-input")
    check("another role's selector picks that role's form",
          res.ok and set(st["uploads"]) == {"recruiter"}, f"ok={res.ok} uploads={st['uploads']}")

    res, st = run(PANELS.as_uri(), "")
    check("no hint, all panels closed -> still refused as ambiguous, nothing written",
          not res.ok and "ambiguous" in res.detail and st["uploads"] == {}, res.detail[:80])
    check("the candidates name each input's form so the agent can choose",
          sorted(c.split(" in form#")[-1] for c in res.candidates)
          == ["apply-form", "creator-form", "engineer-form", "recruiter-form"], str(res.candidates)[:120])
    res, st = run(PANELS.as_uri(), "input#cv-input")
    check("a selector that matches nothing picks nothing (still ambiguous)",
          not res.ok and "ambiguous" in res.detail and st["uploads"] == {}, res.detail[:80])
    res, st = run(PANELS.as_uri() + "?open=engineer", "")
    check("panel open, no hint -> the rendered input (unchanged behaviour)",
          res.ok and st["uploads"] == {"engineer": 1}, f"ok={res.ok} uploads={st['uploads']}")

    print("\nUnit: selector picks")
    a = uploader.UploadTarget(1, {"type": "file", "class": "cv-input"}, "t", "ws", hint_hits=(1, 1, 0))
    b = uploader.UploadTarget(2, {"type": "file", "class": "cv-input"}, "t", "ws", hint_hits=(0, 0, 0))
    check("hits on one of two inputs pick it", uploader._selector_pick([a, b]) == {id(a)})
    c = uploader.UploadTarget(3, {"type": "file"}, "t", "ws", hint_hits=(1, 0, -1))
    d = uploader.UploadTarget(4, {"type": "file"}, "t", "ws", hint_hits=(1, 1, -1))
    check("a selector matching every input narrows nothing by itself",
          uploader._selector_pick([c, d]) == {id(d)})
    e = uploader.UploadTarget(5, {"type": "file"}, "t", "ws", hint_hits=(1, 0))
    f = uploader.UploadTarget(6, {"type": "file"}, "t", "ws", hint_hits=(0, 1))
    check("selectors that disagree pick nothing", uploader._selector_pick([e, f]) is None)
    check("id-like words become #id selectors",
          uploader._hint_selectors("inside engineer-form") == ["inside engineer-form", "#engineer-form"])
    check("selectors written inside prose are tried on their own",
          uploader._hint_selectors('under "Apply" in form#engineer-form')[1:] == ["#engineer-form"])
    res, st = run(PANELS.as_uri(), "#engineer-form .cv-input")
    check("the success message names the form the file went into",
          res.ok and "in form#engineer-form" in res.detail, res.detail[:120])


def _hirebridge(base: str) -> None:
    print("\nHirebridge 2396: the input's onchange posts the whole form back")
    page = f"{base}/v3/ApplicationV2/QuickApply.aspx"
    for echo in ("visible", "markup"):
        POSTS.clear()
        res, st = run(f"{page}?echo={echo}", "", settle=0.8)
        check(f"echo={echo}: success once the reloaded page carries the file",
              res.ok and "posted the form back" in res.detail, res.detail[:110])
        check(f"echo={echo}: exactly one post-back, with our file", POSTS == [RESUME.name], f"posts={POSTS}")
        check(f"echo={echo}: the server-side page state names the file",
              st and st["name"] == RESUME.name, f"state={st}")
    res, st = run(f"{page}?echo=visible", 'name="ctl00$pageContent$ResumeUpload1$uploadedresume"', settle=0.8)
    check("the agent's name= hint works the same", res.ok, res.detail[:90])

    POSTS.clear()
    res, st = run(f"{page}?echo=none", "", settle=1.5)
    check("server drops the file -> UPLOAD_FAILED, never a false success",
          not res.ok and "posted the form back" in res.detail, res.detail[:120])
    check("re-resolved once and wrote once more: at most 2 post-backs",
          1 <= len(POSTS) <= 2 and all(p == RESUME.name for p in POSTS), f"posts={POSTS}")

    POSTS.clear()
    res, st = run(f"{page}?echo=visible&move=1", "", settle=0.8)
    check("a post-back that lands on another address is still refused as scope drift",
          not res.ok and "scope drift" in res.detail, res.detail[:120])
    check("...after a single post-back", len(POSTS) == 1, f"posts={POSTS}")


def _munson() -> None:
    print("\nMunson 2623 (Eightfold): Data Privacy Notice with Close | Cancel | I understand")
    res, st = run(SITES.as_uri() + "?site=eightfold&notice=1", "", settle=1.0)
    check("upload succeeds after the notice is accepted",
          res.ok and st["uploads"] == {"resume": 1} and st["chips"] == [RESUME.name],
          f"ok={res.ok} uploads={st['uploads']} {res.detail[:60]}")
    check("only 'I understand' was clicked (not Close, not Cancel)", st["clicks"] == ["agree"],
          f"clicks={st['clicks']}")
    check("one write: no second write reopened the notice", st.get("changes") == 1 and not st.get("open"),
          f"changes={st.get('changes')} open={st.get('open')}")
    check("the agent is told the notice was accepted",
          'accepted the site\'s "Data Privacy Notice" dialog ("I understand")' in res.detail, res.detail[-120:])
    res, st = run(SITES.as_uri() + "?site=eightfold&notice=1&locked=1", "")
    check("disabled 'I understand' -> UPLOAD_FAILED naming the buttons",
          not res.ok and st["clicks"] == [] and "Cancel | I understand" in res.detail, res.detail[-120:])


def _checks(base: str) -> int:
    _southridge()
    _hirebridge(base)
    _munson()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


def main() -> int:
    global CDP, RESUME
    results.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Stability.setUpClass()
    try:
        CDP = Stability.base
        # Like production: a name unique to the job and its content.
        RESUME = Path(Stability.tmp.name) / "Alex_Example_Resume_2396_a1b2c3.pdf"
        RESUME.write_bytes(Stability.pdf.read_bytes())
        return _checks(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        Stability.tearDownClass()
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
