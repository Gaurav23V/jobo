#!/usr/bin/env python3
"""Opt-in real-agent NEED_CLICK proof; isolated browser and rejecting HTTP server.

Runs real Hermes model calls. No jobs DB writes, inbox access, real applications,
or permanent Hermes config edits. This opt-in test requires
browser.allow_private_urls=true during the run; restore the prior value after.
Run: uv run python scripts/test_click_handoff_e2e.py
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import agent_driver, clicker, form_filler, prompts  # noqa: E402

HTML = b'''<!doctype html><html><head><title>Local consent handoff test</title></head>
<body><h1>Local consent handoff test</h1><p>No uploads or personal details required.</p>
<form id="application"><label for="consent">I consent to the above terms</label>
<input id="consent" name="consent" type="checkbox" required>
<button type="submit">Submit application</button></form><p id="result"></p>
<script>
let trustedConsent = false;
consent.addEventListener('click', e => { trustedConsent = e.isTrusted && consent.checked; });
application.addEventListener('submit', async e => {
 e.preventDefault();
 const response = await fetch('/submit', {method:'POST', headers:{'Content-Type':'application/json'},
 body:JSON.stringify({consent:consent.checked, trustedConsent})});
 const data = await response.json(); result.textContent = data.message;
});
</script></body></html>'''


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    receipts: list[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            with lock:
                body = json.dumps(receipts).encode() if self.path == '/state' else HTML
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if self.path == '/state' else 'text/html')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            accepted = data.get('consent') is True and data.get('trustedConsent') is True
            with lock:
                receipts.append({'accepted': accepted, **data})
            time.sleep(0.5)  # A double-click implementation races this real response.
            self.send_response(200 if accepted else 422)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'message': 'Application submitted' if accepted else
                                         'Required consent was not checked with real input'}).encode())

    with ExitStack() as stack:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        stack.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(server.shutdown)
        profile = stack.enter_context(tempfile.TemporaryDirectory(prefix='jobo-click-e2e-'))
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch_persistent_context(
            profile, headless=True, args=['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0'])
        stack.callback(browser.close)
        port = int((Path(profile) / 'DevToolsActivePort').read_text().splitlines()[0])
        assert port != 9333, 'Refusing production CDP port'
        cdp = f'http://127.0.0.1:{port}'
        url = f'http://127.0.0.1:{server.server_port}/'
        calls: list[dict] = []
        real_click = clicker.click

        def record_click(cdp_url, hint=''):
            assert cdp_url == cdp
            result = real_click(cdp_url, hint)
            calls.append({'hint': hint, 'ok': result.ok, 'delivered': result.delivered,
                          'kind': result.control_kind, 'outcome': result.outcome})
            return result

        stack.enter_context(patch.object(form_filler, '_ensure_browser', return_value=(True, '')))
        stack.enter_context(patch.object(clicker, 'click', side_effect=record_click))
        print(f'Isolated real-agent test: {url} CDP={cdp}', flush=True)
        outcome = agent_driver.run_apply_agent(
            system_prompt=prompts.SYSTEM_PROMPT,
            user_prompt=f'This is an authorized local test, not a real application. Navigate to {url}. '
                        'The form contains only one required consent checkbox and one final submit button. '
                        'No resume, account, email, or personal data is needed. Complete it using the '
                        'normal NEED_CLICK handoffs from your instructions, then verify confirmation. '
                        'Do not access any other website or use credential tools. Report the normal '
                        'STATUS/DETAIL/FIELDS_FILLED_JSON verdict.',
            cdp_url=cdp, job_id=99994,
        )
        # Read HTTP server state back independently of the agent's own verdict.
        import urllib.request
        with urllib.request.urlopen(url + 'state', timeout=5) as response:
            final = json.load(response)
        print(json.dumps({'verdict': outcome.status, 'detail': outcome.detail,
                          'handoffs': calls, 'server_receipts': final}, indent=2), flush=True)
        assert len(final) == 1 and final[0]['accepted'], final
        assert outcome.status == 'submitted', outcome
        assert len(calls) == 2, calls
        assert calls[0]['kind'] == 'checkbox' and calls[0]['ok'], calls
        assert calls[1]['kind'] == 'button' and calls[1]['delivered'], calls
        print('REAL AGENT CONSENT HANDOFF PASSED: checked once, server accepted once, no duplicate.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
