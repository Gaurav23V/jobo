#!/usr/bin/env python3
"""A local ATS-style form whose resume field is a shadow-DOM file input.

This reproduces the exact shape that defeated the pipeline twice: job393
(Treyco / SmartRecruiters `spl-dropzone`) failed on 2026-09-01 AND again on
2026-09-02 after the correct CDP technique was documented in the agent's
prompt. It is the hardest real upload shape we have evidence for:

  * the file input lives inside a shadow root, so document.querySelector and
    CDP's DOM.querySelector cannot see it at all
  * it is styled invisible behind a "Drop or select a file" button
  * the surrounding form is otherwise ordinary, so a failure is unambiguous

The server only accepts the submission if a file with the expected name is
actually attached, so "APPLICATION_SUBMITTED" cannot be faked or hallucinated
by the agent.

Run: uv run python scripts/upload_test_server.py [--port 8898]
Check state: curl -s localhost:8898/state
"""

from __future__ import annotations

import argparse
import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

STATE: dict = {"submitted": False, "filename": None, "fields": {}}
LOCK = threading.Lock()

PAGE = """<!doctype html>
<html><head><title>Selftest ATS - Backend Engineer (shadow upload)</title></head>
<body style="font-family:sans-serif;max-width:680px;margin:40px auto">
<h1>Apply: Backend Engineer</h1>
<p>Shadow Corp - Springfield, India</p>
<form id="app" method="POST" action="/submit" enctype="multipart/form-data">
  <p><label>Full name<br><input name="name" id="name" required style="width:100%"></label></p>
  <p><label>Email<br><input name="email" id="email" type="email" required style="width:100%"
     value=""></label></p>
  <p><label>Phone<br><input name="phone" id="phone" style="width:100%"></label></p>

  <p><strong>Resume / CV (required)</strong><br>
  <!-- The real input is inside this component's shadow root. -->
  <spl-dropzone id="resume-zone"></spl-dropzone>
  </p>

  <p><label><input type="checkbox" name="consent" id="consent" required> I agree to the
     processing of my data</label></p>
  <button type="submit" id="submit">Submit application</button>
</form>
<hr>
<h3>Attached</h3>
<pre id="log">(nothing yet)</pre>

<script>
// Minimal stand-in for SmartRecruiters' spl-dropzone web component: a styled
// button in the light DOM, the actual <input type=file> hidden in a shadow root.
class SplDropzone extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = `
      <style>
        .zone { border: 2px dashed #888; padding: 18px; border-radius: 6px; cursor: pointer; }
        input { position: absolute; width: 1px; height: 1px; opacity: 0; }
      </style>
      <div class="zone" id="zone">Drop or select a file</div>
      <input type="file" id="file-input" name="resume" accept=".pdf,.doc,.docx">
    `;
    const input = root.getElementById('file-input');
    root.getElementById('zone').addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
      const names = Array.from(input.files || []).map(f => f.name);
      document.getElementById('log').textContent = JSON.stringify(names);
      root.getElementById('zone').textContent = names.length
        ? 'Attached: ' + names.join(', ') : 'Drop or select a file';
    });
  }
  get files() {
    const el = this.shadowRoot && this.shadowRoot.getElementById('file-input');
    return el ? el.files : null;
  }
}
customElements.define('spl-dropzone', SplDropzone);

// The form must carry the shadow input's file, since shadow inputs are not
// automatically part of form submission.
document.getElementById('app').addEventListener('submit', (e) => {
  const zone = document.getElementById('resume-zone');
  const files = zone.files;
  if (!files || !files.length) {
    e.preventDefault();
    alert('Please attach your resume.');
    return;
  }
  const dt = new DataTransfer();
  dt.items.add(files[0]);
  let proxy = document.getElementById('proxy-file');
  if (!proxy) {
    proxy = document.createElement('input');
    proxy.type = 'file';
    proxy.name = 'resume';
    proxy.id = 'proxy-file';
    proxy.style.display = 'none';
    document.getElementById('app').appendChild(proxy);
  }
  proxy.files = dt.files;
});

window.__attached = () => {
  const zone = document.getElementById('resume-zone');
  const files = zone && zone.files;
  return JSON.stringify(Array.from(files || []).map(f => f.name));
};
</script>
</body></html>
"""

SUCCESS = """<!doctype html><html><body style="font-family:sans-serif;max-width:680px;margin:40px auto">
<h2 style="color:#060">APPLICATION_SUBMITTED</h2>
<p>Thank you, %(name)s. We received your application.</p>
<p>Resume on file: <strong>%(filename)s</strong></p>
<p>Reference: SHADOW-0001</p>
</body></html>"""

REJECT = """<!doctype html><html><body style="font-family:sans-serif;max-width:680px;margin:40px auto">
<h2 style="color:#a00">SUBMISSION_REJECTED</h2>
<p>No resume file was attached. The application was not accepted.</p>
<p><a href="/">Back to the form</a></p>
</body></html>"""


def parse_multipart(raw: bytes, ctype: str) -> tuple[str | None, dict[str, str]]:
    """Minimal multipart/form-data parser.

    Hand-rolled because `cgi` is removed in Python 3.13 and this test server
    should not depend on a module that no longer exists. Only needs to handle
    what a browser form post produces: flat fields plus one file part.
    Returns (uploaded_filename_or_None, {field: value}).
    """
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not m:
        return None, {}
    boundary = m.group(1).encode()
    parts = raw.split(b"--" + boundary)
    filename: str | None = None
    fields: dict[str, str] = {}

    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, body = part.split(b"\r\n\r\n", 1)
        head_text = head.decode("utf-8", errors="replace")
        name_m = re.search(r'name="([^"]*)"', head_text)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', head_text)
        body = body.rstrip(b"\r\n")
        if file_m:
            # An empty filename means the input was present but had no file.
            if file_m.group(1) and body:
                filename = file_m.group(1)
        else:
            fields[name] = body.decode("utf-8", errors="replace")
    return filename, fields


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        print(f"  [server] {format % args}")

    def _send(self, body: str, code: int = 200):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/apply"):
            with LOCK:
                if STATE["submitted"]:
                    self._send(
                        SUCCESS
                        % {
                            "name": html.escape(STATE["fields"].get("name", "")),
                            "filename": html.escape(STATE["filename"] or ""),
                        }
                    )
                    return
            self._send(PAGE)
        elif path == "/state":
            with LOCK:
                payload = json.dumps(STATE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send("<p>Not found</p>", 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/submit":
            self._send("<p>Not found</p>", 404)
            return
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length)

        filename, fields = parse_multipart(raw, ctype)

        if not filename:
            print("  [server] REJECTED: no file attached")
            self._send(REJECT, 200)
            return

        with LOCK:
            STATE["submitted"] = True
            STATE["filename"] = filename
            STATE["fields"] = fields
        print(f"  [server] ACCEPTED with resume={filename!r} fields={list(fields)}")
        self._send(SUCCESS % {"name": html.escape(fields.get("name", "")), "filename": html.escape(filename)})


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded: a browser keep-alive connection must not block /state polls."""

    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8898)
    args = ap.parse_args()
    server = ThreadedHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Shadow-DOM upload test ATS on http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
