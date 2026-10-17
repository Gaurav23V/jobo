#!/usr/bin/env python3
"""A local ATS form whose required field is a select2 dropdown.

Reproduces the widget that became the top failure once uploads were fixed: on
the 2026-09-04 run, 5 of 26 stuck jobs died on select2 autocompletes (Avature
"college name", Wingtip "School", SuccessFactors "Country/Region Code"), each
burning the agent's ENTIRE turn budget on a single field. A transcript named
the cause exactly: "clicked the dropdown without registering the selection -
the underlying hidden <select id='10955'> never updated".

The server REJECTS any submission whose college field is empty, so the agent
cannot pass this by ignoring the dropdown.

Run: uv run python scripts/dropdown_test_server.py [--port 8897]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs

STATE: dict = {"submitted": False, "fields": {}}
LOCK = threading.Lock()

PAGE = """<!doctype html>
<html><head><title>Selftest ATS - Backend Engineer</title></head>
<body style="font-family:sans-serif;max-width:680px;margin:40px auto">
<h1>Apply: Backend Engineer</h1>
<p>Dropdown Corp - Springfield, India</p>
<form id="app" method="POST" action="/submit">
  <p><label>Full name<br><input name="name" id="name" required style="width:100%"></label></p>
  <p><label>Email<br><input name="email" id="email" type="email" required
     style="width:100%"></label></p>

  <p><strong>College Name (required)</strong><br>
  <!-- select2 pattern: a fake combobox over a hidden real select -->
  <div class="select2-container">
    <input id="10955_input" role="combobox" autocomplete="off"
           placeholder="Search for your college..." style="width:100%">
    <select id="10955" name="college" required style="display:none">
      <option value="">-- select --</option>
      <option value="LIIT Springfield">LIIT Springfield</option>
      <option value="IIT Delhi">IIT Delhi</option>
      <option value="NIT Riverton">NIT Riverton</option>
    </select>
  </div>
  </p>

  <p><label>Notice period<br>
    <select name="notice" id="notice" required>
      <option value="">-- select --</option>
      <option value="immediate">Immediate</option>
      <option value="30 days">30 days</option>
      <option value="60 days">60 days</option>
    </select>
  </label></p>

  <p><label><input type="checkbox" name="consent" id="consent" required>
     I agree to the processing of my data</label></p>
  <button type="submit" id="submit">Submit application</button>
</form>
<hr><pre id="state"></pre>
<script>
// The fake combobox deliberately does NOT write through to the hidden select
// when typed into - exactly like the real widgets that defeated the agent.
// Only a proper value assignment + change event on the hidden select works.
const combo = document.getElementById('10955_input');
const hidden = document.getElementById('10955');
hidden.addEventListener('change', () => { combo.value = hidden.value; });
combo.addEventListener('input', () => { /* intentionally inert */ });

document.getElementById('app').addEventListener('submit', (e) => {
  if (!hidden.value) { e.preventDefault(); alert('Please select your college.'); }
});
window.__state = () => JSON.stringify({
  college: hidden.value, notice: document.getElementById('notice').value
});
setInterval(() => {
  document.getElementById('state').textContent = window.__state();
}, 300);
</script>
</body></html>
"""

SUCCESS = """<!doctype html><html><body style="font-family:sans-serif;max-width:680px;margin:40px auto">
<h2 style="color:#060">APPLICATION_SUBMITTED</h2>
<p>Thank you, %(name)s.</p>
<p>College on file: <strong>%(college)s</strong></p>
<p>Reference: DROPDOWN-0001</p></body></html>"""

REJECT = """<!doctype html><html><body style="font-family:sans-serif;max-width:680px;margin:40px auto">
<h2 style="color:#a00">SUBMISSION_REJECTED</h2>
<p>The college field was empty. The application was not accepted.</p>
<p><a href="/">Back</a></p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
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
                            "college": html.escape(STATE["fields"].get("college", "")),
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
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        college = (fields.get("college") or "").strip()
        if not college:
            print("  [server] REJECTED: college empty")
            self._send(REJECT)
            return
        with LOCK:
            STATE["submitted"] = True
            STATE["fields"] = fields
        print(f"  [server] ACCEPTED college={college!r} fields={list(fields)}")
        self._send(
            SUCCESS
            % {"name": html.escape(fields.get("name", "")), "college": html.escape(college)}
        )


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8897)
    args = ap.parse_args()
    server = ThreadedHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Dropdown test ATS on http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
