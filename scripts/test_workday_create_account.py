#!/usr/bin/env python3
"""Workday Create Account that "does nothing": NEED_CLICK must say why.

GHO-21 (Starling 1646, Heron 1648, 2026-10-15 14:48-14:56; Adatum 1513 on
2026-10-11): Create Account, Sign In and Forgot your password? all came back
"one click sent; submission unconfirmed", the page never changed, and the job
was dropped as an "ATS account wall". Measured on scratch Chromium against the
live heron.wd5 form (2026-10-15):

  * The click lands on the click_filter overlay and Workday runs the form:
    empty fields -> "Error: Please enter a valid email"; mismatched passwords
    -> "Error: Passwords do not match"; valid ones -> POST /register (6/6,
    with values typed or set by agent-browser's fill). An existing account
    gets 303 -> /login. No consent box on Heron; Adatum had it ticked.
  * When /register fails (aborted, 403, 500 or never answered) Workday shows
    NOTHING: same form, no error, no spinner. That is the reported no-op.
  * No "Verify your candidate account" mail from Starling ever arrived, so the
    production request never reached Workday's server.
  * The clicker could not tell any of these apart, and it reported Workday's
    own "Error: ..." validation as "unknown" too.

Now the clicker watches the page's own same-site requests after the press,
and whether the page saw the trusted click, and says which case it is.

Self-isolated: own headless Chromium on a random port (never 9333), fixture
served from a local http.server, no live site, browser, jobs, DB or agent.
Run: bash scripts/run_regression_tests.sh test_workday_create_account
"""
from __future__ import annotations

import http.server
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import clicker, form_filler  # noqa: E402

# Measured shape of Workday's Create Account panel: the overlay forwards the
# click to the aria-hidden submit; the app validates, then POSTs /register.
# `mode` picks how the fixture app reacts, so one page covers every case.
PAGE = """<!doctype html><html><head><style>
  body { margin: 0; font: 14px sans-serif; }
  .wrap { position: relative; width: 240px; height: 40px; margin: 8px; }
  .native { width: 240px; height: 40px; }
  .filter { position: absolute; inset: 0; z-index: 3; cursor: pointer; }
</style></head><body>
<h3>Create Account</h3>
<form id="f" onsubmit="event.preventDefault()">
  <input id="email" aria-label="Email Address" value="jobo.probe@example.com">
  <input id="pw" type="password" aria-label="Password" value="Abcdefgh#12345">
  <input id="pw2" type="password" aria-label="Verify New Password" value="MODE_PW2">
  <p id="err"></p>
  <div class="wrap">
    <div role="button" tabindex="0" aria-label="Create Account" data-automation-id="click_filter"
         class="filter"></div>
    <button type="submit" tabindex="-2" aria-hidden="true" class="native"
            data-automation-id="createAccountSubmitButton">Create Account</button>
  </div>
</form>
<script>
  const mode = 'MODE';
  document.querySelector('[data-automation-id=click_filter]').addEventListener('click', () => {
    if (pw.value !== pw2.value) { err.textContent = 'Error: Passwords do not match'; return; }
    if (mode === 'ignore') return;
    fetch('/register?mode=' + mode, {method: 'POST', body: 'username=' + email.value,
                                      redirect: 'manual'})
      .then(r => { if (r.type === 'opaqueredirect') location.assign('/login'); })
      .catch(() => {});  // Workday: a failed register shows nothing at all
  });
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        mode = self.path.split("/", 2)[1] if self.path.count("/") >= 1 else ""
        body = (PAGE.replace("MODE_PW2", "Mismatch#9999" if mode == "validation" else "Abcdefgh#12345")
                .replace("MODE", mode) if self.path != "/login" else "<h3>Sign In</h3>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        mode = self.path.rsplit("=", 1)[-1]
        if mode == "hang":
            time.sleep(clicker.SETTLE_SEC + 3)
        if mode == "exists":
            self.send_response(303)
            self.send_header("Location", "/login")
        else:
            self.send_response(403 if mode == "blocked" else 200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestWorkdayCreateAccount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.stack.callback(server.shutdown)
        cls.base = f"http://127.0.0.1:{server.server_address[1]}"
        profile = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="jobo-wd-create-"))
        pw = cls.stack.enter_context(sync_playwright())
        cls.context = pw.chromium.launch_persistent_context(
            profile, headless=True, viewport={"width": 1000, "height": 585},
            args=["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"],
        )
        cls.stack.callback(cls.context.close)
        port = int((Path(profile) / "DevToolsActivePort").read_text().splitlines()[0])
        if port == 9333 or port >= 9342 and port < 9400:
            raise RuntimeError(f"Refusing production-range CDP port {port}")
        cls.cdp_url = f"http://127.0.0.1:{port}"
        cls.page = cls.context.pages[0]
        cls.stack.enter_context(patch.object(form_filler, "_ensure_browser", return_value=(True, "")))
        original_targets = clicker._page_targets

        def isolated_targets(url):
            if url != cls.cdp_url:
                raise AssertionError(f"Non-isolated CDP requested: {url}")
            return original_targets(url)

        cls.stack.enter_context(patch.object(clicker, "_page_targets", side_effect=isolated_targets))
        print(f"Isolated Chromium CDP: {cls.cdp_url}", flush=True)

    def act(self, mode: str):
        self.page.goto(f"{self.base}/{mode}/apply/applyManually")
        result = clicker.click(self.cdp_url, "Create Account")
        self.evidence = f"result={result!r}"
        print(f"\n{mode}: {self.evidence}", flush=True)
        return result

    def test_register_refused_is_reported_not_unknown(self):
        # The production no-op: Workday leaves the form as it was.
        result = self.act("blocked")
        self.assertTrue(result.delivered, self.evidence)
        self.assertTrue(result.received, self.evidence)
        self.assertEqual(result.outcome, "rejected", self.evidence)
        self.assertIn("POST /register -> 403", result.note, self.evidence)
        message = result.as_agent_message()
        self.assertTrue(message.startswith("CLICK_FAILED"), message)
        self.assertIn("server refused", message)

    def test_no_request_sent_is_reported(self):
        result = self.act("ignore")
        self.assertEqual(result.outcome, "unknown", self.evidence)
        self.assertTrue(result.received, self.evidence)
        self.assertEqual(result.requests, [], self.evidence)
        self.assertIn("received the click but sent no request", result.note, self.evidence)

    def test_pending_register_stays_do_not_retry(self):
        result = self.act("hang")
        self.assertEqual(result.outcome, "unknown", self.evidence)
        self.assertIn("POST /register -> no response yet", result.note, self.evidence)
        self.assertIn("do not retry", result.note, self.evidence)
        self.assertIn("Do NOT click again", result.as_agent_message())

    def test_workday_error_text_is_validation(self):
        result = self.act("validation")
        self.assertEqual(result.outcome, "validation", self.evidence)
        self.assertIn("error:", result.changed, self.evidence)
        self.assertEqual(result.requests, [], self.evidence)

    def test_existing_account_redirect_navigates(self):
        result = self.act("exists")
        self.assertTrue(result.ok, self.evidence)
        self.assertEqual(result.outcome, "navigation", self.evidence)
        self.assertTrue(result.after_url.endswith("/login"), self.evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
