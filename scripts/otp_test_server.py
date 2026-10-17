#!/usr/bin/env python3
"""A local ATS-style form gated by an emailed verification code.

Exists to test the Module 4 OTP handoff end to end for real: a live agent, the
real persistent browser, a real code delivered to the real Gmail inbox. The
only synthetic part is who sends the mail, because we cannot make Northwind
issue codes on demand.

Flow (mirrors Azure B2C / Workday / SuccessFactors):
  GET  /            -> form with name/email + "Send verification code"
  POST /send-code   -> generates a 6-digit code, INSERTS a real verification
                       email into the user's Gmail via the same API the collector
                       uses, and reveals the code input
  POST /verify      -> accepts the code; wrong code re-prompts
  -> success page containing the literal string APPLICATION_SUBMITTED

Run: uv run python scripts/otp_test_server.py [--port 8899]
Then point an agent at http://127.0.0.1:8899/
"""

from __future__ import annotations

import argparse
import base64
import html
import random
import sys
import threading
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import otp_lookup  # noqa: E402

SENDER_DOMAIN = "jobo-ats-selftest.example.com"

STATE: dict = {"code": None, "sent": False, "submitted": False, "message_ids": []}
LOCK = threading.Lock()

PAGE = """<!doctype html>
<html><head><title>Selftest ATS - Backend Engineer</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto;">
<h1>Apply: Backend Engineer</h1>
<p>Selftest Corp - Springfield, India</p>
{body}
</body></html>
"""

FORM_STEP1 = """
<form method="POST" action="/send-code">
  <p><label>Full name<br><input name="name" id="name" required style="width:100%%"></label></p>
  <p><label>Email<br><input name="email" id="email" type="email" required
     value="alex.example@example.com" style="width:100%%"></label></p>
  <p>To continue, we must verify your email address.</p>
  <button type="submit" id="send-code">Send verification code</button>
</form>
"""

FORM_STEP2 = """
<p style="color:#060;">A verification code has been emailed to
<strong>%(email)s</strong>. Enter it below to submit your application.</p>
<form method="POST" action="/verify">
  <p><label>Verification code<br><input name="code" id="code" required
     autocomplete="off" style="width:200px"></label></p>
  <button type="submit" id="verify">Verify and submit application</button>
</form>
%(error)s
"""

SUCCESS = """
<h2 style="color:#060;">APPLICATION_SUBMITTED</h2>
<p>Thank you for applying, %(name)s. Your application has been received.</p>
<p>Reference: SELFTEST-%(ref)s</p>
"""


def _send_code_email(code: str) -> str:
    """Insert a real verification email into the mailbox."""
    service = otp_lookup._gmail_service()
    body = (
        "Hello,\n\n"
        "Please use the following verification code to continue your "
        "application:\n\n"
        f"    {code}\n\n"
        "This code expires in 15 minutes.\n"
        "Selftest Corp Careers\n"
    )
    msg = MIMEText(body)
    msg["To"] = "alex.example@example.com"
    msg["From"] = f"Selftest Careers <no-reply@{SENDER_DOMAIN}>"
    msg["Subject"] = "Verify your email address"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    inserted = (
        service.users()
        .messages()
        .insert(userId="me", body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]})
        .execute()
    )
    return inserted["id"]


def cleanup_messages() -> int:
    """Trash every verification mail this server inserted."""
    if not STATE["message_ids"]:
        return 0
    removed = 0
    try:
        service = otp_lookup._gmail_service()
    except Exception:
        return 0
    for mid in STATE["message_ids"]:
        for verb in ("delete", "trash"):
            try:
                getattr(service.users().messages(), verb)(userId="me", id=mid).execute()
                removed += 1
                break
            except Exception:
                continue
    return removed


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature set by stdlib
        print(f"  [server] {format % args}")

    def _respond(self, body: str, code: int = 200):
        payload = PAGE.format(body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/apply"):
            with LOCK:
                if STATE["submitted"]:
                    self._respond(SUCCESS % {"name": "Alex Example", "ref": "0001"})
                elif STATE["sent"]:
                    self._respond(
                        FORM_STEP2 % {"email": "alex.example@example.com", "error": ""}
                    )
                else:
                    self._respond(FORM_STEP1)
        elif self.path == "/state":
            import json as _json

            with LOCK:
                payload = _json.dumps(
                    {k: v for k, v in STATE.items() if k != "message_ids"}
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._respond("<p>Not found.</p>", code=404)

    def do_POST(self):
        form = self._form()
        if self.path == "/send-code":
            code = f"{random.randint(100000, 999999)}"
            try:
                mid = _send_code_email(code)
            except Exception as e:
                self._respond(f"<p style='color:#a00'>Mail send failed: {html.escape(str(e))}</p>")
                return
            with LOCK:
                STATE["code"] = code
                STATE["sent"] = True
                STATE["message_ids"].append(mid)
                STATE["name"] = form.get("name", "Alex Example")
            print(f"  [server] emailed code {code} (message {mid})")
            self._respond(FORM_STEP2 % {"email": form.get("email", ""), "error": ""})
        elif self.path == "/verify":
            supplied = (form.get("code") or "").strip()
            with LOCK:
                expected = STATE["code"]
                name = STATE.get("name", "Alex Example")
            if supplied and supplied == expected:
                with LOCK:
                    STATE["submitted"] = True
                print(f"  [server] code {supplied} accepted -> APPLICATION_SUBMITTED")
                self._respond(SUCCESS % {"name": html.escape(name), "ref": "0001"})
            else:
                print(f"  [server] rejected code {supplied!r} (expected {expected!r})")
                self._respond(
                    FORM_STEP2
                    % {
                        "email": "alex.example@example.com",
                        "error": "<p style='color:#a00'>That code is not correct.</p>",
                    }
                )
        else:
            self._respond("<p>Not found.</p>", code=404)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded so a browser's keep-alive connection cannot block /state.

    The single-threaded HTTPServer deadlocked the first live test run: Chromium
    holds its connection open, so the harness's /state poll waited forever
    behind it and the test timed out even though the application had actually
    been submitted.
    """

    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    server = ThreadedHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"OTP test ATS listening on http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down; cleaning up inserted mail...")
        print(f"  removed {cleanup_messages()} message(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
