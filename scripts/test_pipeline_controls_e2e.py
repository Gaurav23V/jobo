#!/usr/bin/env python3
"""Opt-in full-agent regression: popup + long form + five repaired mechanisms.

Uses an isolated browser and HTTP server, never jobs.db or production CDP9333.
Requires browser.allow_private_urls=true in the Hermes profile the agent runs
in (the `jobo` profile, see scripts/setup_hermes_profile.sh) for the test only;
restore its prior setting afterward. Run: uv run python scripts/test_pipeline_controls_e2e.py
Optional --hermes-bin tests a separate checked-out Hermes CLI before installing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import agent_driver, clicker, form_filler, prompts, upload_names, uploader  # noqa: E402


def verify_browser_observations(export_path: Path, apply_url: str) -> list[dict]:
    """Check real stored tool returns across ALL resume subprocesses, not prose."""
    session = json.loads(export_path.read_text())
    form_seen = False
    observations = []
    failures = []
    for message in session['messages']:
        # Click/scroll/press results carry the page after the action (GHO-15).
        if message.get('role') != 'tool' or message.get('tool_name') not in (
                'browser_snapshot', 'browser_click', 'browser_scroll', 'browser_press'):
            continue
        raw = message.get('content', '')
        start = raw.find('{')
        if start < 0:
            continue
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
        snapshot = data.get('snapshot', '')
        if not snapshot:
            continue
        is_form = 'Backend Engineer at Local Test Company' in snapshot
        if is_form:
            form_seen = True
        if not form_seen:
            continue
        top = (data.get('frame_tree') or {}).get('top')
        observed = {'form': is_form, 'old_source': 'Local job listing' in snapshot,
                    'metadata_url': top.get('url') if top else None}
        observations.append(observed)
        if observed['old_source']:
            failures.append('snapshot returned the old source tab after a handoff')
        if top and top.get('url') != apply_url:
            failures.append('snapshot frame metadata belongs to another page')
    assert form_seen and observations, 'No application snapshot observed'
    assert not failures, {'failures': failures, 'observations': observations}
    return observations


def session_usage(export_path: Path) -> dict:
    """Model calls, prompt tokens and snapshot sizes of the exported session."""
    session = json.loads(export_path.read_text())
    meta = session.get('session') or session
    snaps = [len(m.get('content') or '') for m in session['messages']
             if m.get('role') == 'tool' and m.get('tool_name') in ('browser_snapshot', 'browser_navigate')]
    keys = ('api_call_count', 'input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens')
    usage = {k: meta.get(k) for k in keys}
    usage['prompt_tokens'] = sum(int(meta.get(k) or 0) for k in keys[1:4])
    usage['snapshot_results'] = len(snaps)
    usage['snapshot_chars_avg'] = int(sum(snaps) / len(snaps)) if snaps else 0
    return usage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-bin')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    resume = ROOT / 'personal.example/resume.pdf'
    expected_hash = hashlib.sha256(resume.read_bytes()).hexdigest()
    fixture = Path(__file__).with_name('pipeline_controls_probe.html').read_text()
    # Long enough to trigger truncation without adding instructions to the page.
    description = '<p>We build reliable backend software and maintain services for our customers.</p>' * 500
    form_html = fixture.replace('{{LONG_DESCRIPTION}}', description).encode()
    receipts: list[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == '/state':
                with lock:
                    data = json.dumps(receipts).encode()
                content_type = 'application/json'
            elif self.path == '/apply':
                data, content_type = form_html, 'text/html; charset=utf-8'
            else:
                data = (b'<!doctype html><title>Job listing source</title><h1>Local job listing</h1>'
                        b'<a href="/apply" target="_blank">Apply on company website</a>')
                content_type = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            if self.path != '/submit' or not 0 < length < 2_000_000:
                self.send_error(400)
                return
            data = json.loads(self.rfile.read(length))
            fields = data.get('fields', {})
            raw = base64.b64decode(data.get('file', ''), validate=True)
            digest = hashlib.sha256(raw).hexdigest()
            failures = []
            for key, wanted in {'degree': 'Bachelors', 'year': '2022', 'previous': 'No', 'currentCompany': '', 'consent': True}.items():
                if fields.get(key) != wanted:
                    failures.append(key)
            # The pipeline uploads a byte-identical copy named for the job
            # (module4/upload_names.py); the bytes are what must match.
            if digest != expected_hash or data.get('filename') != upload_names.upload_name(99993, resume):
                failures.append('resume')
            if fields.get('submits') != 1:
                failures.append('duplicate submission')
            accepted = not failures
            with lock:
                receipts.append({'accepted': accepted, 'fields': fields,
                                 'filename': data.get('filename'), 'sha256': digest,
                                 'failures': failures})
            time.sleep(0.45)
            self.send_response(200 if accepted else 422)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'message': 'Application submitted' if accepted else
                                        'Required fields not committed: ' + ', '.join(failures)}).encode())

    with ExitStack() as stack:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        stack.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        profile = stack.enter_context(tempfile.TemporaryDirectory(prefix='jobo-full-e2e-'))
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch_persistent_context(
            profile, headless=True, viewport={'width': 1000, 'height': 720},
            args=['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0'])
        stack.callback(browser.close)
        port = int((Path(profile) / 'DevToolsActivePort').read_text().splitlines()[0])
        assert port != 9333
        cdp = f'http://127.0.0.1:{port}'
        calls: list[dict] = []
        agent_sessions: list[str] = []
        real_run = agent_driver._run_hermes
        def record_run(**kwargs):
            output, timed_out = real_run(**kwargs)
            agent_sessions.extend(re.findall(r'^session_id:\s*(\S+)', output, re.MULTILINE))
            return output, timed_out
        stack.enter_context(patch.object(agent_driver, '_run_hermes', side_effect=record_run))

        def instrument(module, name):
            original = getattr(module, name)
            def invoke(*pos, **kw):
                assert pos[0] == cdp, 'Unexpected CDP target'
                result = original(*pos, **kw)
                calls.append({'helper': name, 'ok': result.ok,
                              'request': str(pos[1:])[:140],
                              'strategy': getattr(result, 'strategy', ''),
                              'note': getattr(result, 'note', '')})
                return result
            stack.enter_context(patch.object(module, name, side_effect=invoke))

        # Isolate liveness recovery only; all DOM operations and the model run are real.
        stack.enter_context(patch.object(form_filler, '_ensure_browser', return_value=(True, '')))
        instrument(form_filler, 'set_field')
        instrument(uploader, 'upload_file')
        instrument(clicker, 'click')
        if args.hermes_bin:
            stack.enter_context(patch.object(agent_driver, '_hermes_bin', return_value=args.hermes_bin))
        prompt = (
            f'This is an authorized local regression test, not a real application. Open {url}/start, '
            'then follow Apply on company website into the new tab. Stay on this local server only. '
            'The application appears after a long description. Verify browser_console location.href '
            'agrees with the application page after opening it, and report a blocker if it does not. '
            'Use normal handoffs: NEED_UPLOAD for the supplied resume, NEED_FIELDS then NEED_FIELD '
            'for Previously employed here? = No, Degree = Bachelors, Education start year = 2022, '
            'and CLEAR the optional Current company field (this test fixture expects it empty). '
            'Use the exact empty-string value "" for clearing. This tests field-helper invocation; '
            'do not set these controls by hand. Use NEED_CLICK for required consent and final submission. '
            'No other personal details, accounts, messages or credential tools are needed. '
            f'Resume asset: {resume}. Confirm the fresh Application submitted message and return '
            'STATUS/DETAIL/FIELDS_FILLED_JSON. Never retry an uncertain submission.'
        )
        print(f'COMPOSITE LIVE TEST url={url} CDP={cdp}', flush=True)
        outcome = agent_driver.run_apply_agent(system_prompt=prompts.SYSTEM_PROMPT,
            user_prompt=prompt, cdp_url=cdp, job_id=99993, resume_path=str(resume),
            source_url=url + '/start')
        with urllib.request.urlopen(url + '/state', timeout=5) as response:
            truth = json.load(response)
        report = {'verdict': outcome.status, 'detail': outcome.detail,
                  'calls': calls, 'server_receipts': truth}
        print(json.dumps(report, indent=2), flush=True)
        assert len(truth) == 1 and truth[0]['accepted'], report
        assert outcome.status == 'submitted', report
        assert sum(x['helper'] == 'set_field' and x['ok'] for x in calls) >= 4, calls
        assert any(x['helper'] == 'upload_file' and x['ok'] for x in calls), calls
        assert any(x['helper'] == 'click' for x in calls), calls
        assert agent_sessions, 'No actual agent session ID captured'
        with tempfile.TemporaryDirectory(prefix='jobo-e2e-transcript-') as transcript_dir:
            export = Path(transcript_dir) / 'session.jsonl'
            profile = agent_driver.hermes_profile()
            subprocess.run([args.hermes_bin or agent_driver._hermes_bin(),
                            *(['-p', profile] if profile else []), 'sessions', 'export',
                            str(export), '--session-id', agent_sessions[-1], '--format', 'jsonl',
                            '--redact', '--yes'], check=True, capture_output=True, text=True, timeout=30)
            print('BROWSER OBSERVATIONS', verify_browser_observations(export, url + '/apply'))
            print('TOKEN USAGE', json.dumps(session_usage(export)), flush=True)
        print('COMPOSITE REAL-AGENT E2E PASSED: server accepted exactly once with all committed values, exact PDF, and consistent browser page across handoffs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
