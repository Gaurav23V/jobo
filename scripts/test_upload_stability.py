#!/usr/bin/env python3
"""Self-contained real-CDP regression. Own Chromium, random port, no user profile.
Run: uv run python scripts/test_upload_stability.py [--red-only]
"""
import json
import http.server
import threading
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import uploader as u

FIXTURE = Path(__file__).with_name('upload_stability_fixture.html')


class Stability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='jobo-upload-stability-')
        root = Path(cls.tmp.name)
        cls.pdf = root / 'probe-resume.pdf'
        cls.pdf.write_bytes(b'%PDF-1.4\n% regression fixture\n%%EOF\n')
        cls.log = open(root / 'chromium.log', 'w')
        binary = shutil.which('chromium') or shutil.which('chromium-browser')
        if not binary:
            raise RuntimeError('Chromium required')
        cls.proc = subprocess.Popen([binary, '--headless=new', '--no-sandbox', '--disable-gpu',
            '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
            f'--user-data-dir={root}/profile', 'about:blank'], stdout=cls.log, stderr=cls.log,
            start_new_session=True)
        deadline = time.monotonic() + 12
        portfile = root / 'profile' / 'DevToolsActivePort'
        while not portfile.exists():
            if cls.proc.poll() is not None or time.monotonic() > deadline:
                cls.proc.terminate()
                raise RuntimeError('Isolated Chromium startup failed')
            time.sleep(.05)
        port = int(portfile.read_text().splitlines()[0])
        assert port != 9333
        cls.base = f'http://127.0.0.1:{port}'
        cls.http('/json/version')
        cls.guard = cls.http('/json/new?about:blank', 'PUT')
        print(f'Isolated CDP {cls.base}', flush=True)

    @classmethod
    def tearDownClass(cls):
        from scripts.run_isolated_regressions import kill_owned_browsers
        root = Path(cls.tmp.name)
        # Capture ancestry while the browser is alive, then signal exact pidfds.
        # Chromium was launched with port=0; the actual CDP port is in its file.
        try:
            kill_owned_browsers(root, 0, root / 'profile')
        finally:
            try:
                cls.proc.wait(timeout=5)
            finally:
                try:
                    cls.log.close()
                finally:
                    cls.tmp.cleanup()

    @classmethod
    def http(cls, path, method=None):
        with urllib.request.urlopen(urllib.request.Request(cls.base + path, method=method), timeout=3) as r:
            return json.load(r)

    def setUp(self):
        self.tabs = []

    def tearDown(self):
        # Guard remains open throughout; never close the browser's last page.
        for tab in self.tabs:
            with urllib.request.urlopen(self.base + '/json/close/' + tab['id'], timeout=3) as r:
                r.read()
        self.assertIsNone(self.proc.poll())
        self.assertTrue(self.http('/json/version').get('Browser'))

    def ev(self, tab, expression):
        with u._Cdp(tab['webSocketDebuggerUrl']) as c:
            response = c.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
            self.assertNotIn('exceptionDetails', response.get('result', {}))
            return response.get('result', {}).get('result', {}).get('value')

    def tab(self, mode='plain', delay=1200):
        tab = self.http('/json/new?' + FIXTURE.as_uri(), 'PUT'); self.tabs.append(tab)
        deadline = time.monotonic() + 3
        while not self.ev(tab, "typeof window.setup==='function'"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        self.ev(tab, f'window.setup({json.dumps(mode)}, {delay})')
        return tab

    def run_upload(self, **kwargs):
        calls = []
        original = u._Cdp.call
        def counted(cdp, method, params=None):
            if method == 'DOM.setFileInputFiles': calls.append(params)
            return original(cdp, method, params)
        start = time.monotonic()
        with patch.object(u._Cdp, 'call', counted):
            result = u.upload_file(self.base, str(self.pdf), **kwargs)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, getattr(u, 'UPLOAD_BUDGET_SEC', 8) + .7)
        self.assertLessEqual(len(calls), 2, calls)
        print(f'  {self.id().split(".")[-1]}: ok={result.ok} writes={len(calls)} elapsed={elapsed:.3f}s {result.detail}', flush=True)
        return result, calls

    def files(self, tab, slot='resume'):
        return [x['files'] for x in self.ev(tab, 'window.state()')['inputs'] if x['slot']=='input-'+slot]

    def test_delayed_placeholder(self):
        tab = self.tab('late')
        result, _ = self.run_upload()
        # This read happens after the known replacement, even on the broken fast path.
        deadline = time.monotonic() + 3
        while not self.ev(tab, 'window.mounted'):
            self.assertLess(time.monotonic(), deadline); time.sleep(.05)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self.files(tab), [[self.pdf.name]], 'false success: final live slot is empty')
        self.assertEqual(self.files(tab, 'cover'), [[]])

    def test_review_shared_identity_disappears(self):
        tab = self.tab('sharedidentity', 500)
        result, calls = self.run_upload(hint='resume-main')
        self.assertFalse(result.ok, result.detail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ev(tab, "document.getElementById('identity-document').files.length"), 0)

    def test_review_external_form_owner_changes(self):
        tab = self.tab('externalform', 500)
        result, calls = self.run_upload()
        self.assertEqual(self.ev(tab, "document.querySelector('[data-testid=input-resume]').form.id"), 'applicationB')
        self.assertFalse(result.ok, result.detail)
        self.assertEqual(len(calls), 1)

    def test_review_external_form_stable(self):
        tab = self.tab('externalstable')
        result, calls = self.run_upload()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.files(tab), [[self.pdf.name]])

    def test_responsive_twin_form(self):
        # Mapleton 1587/1588: identical input#resume_field in a hidden mobile
        # form and the visible one. Was "actual form owner changed; refusing
        # scope drift" on every request, after selecting the HIDDEN copy.
        tab = self.tab('responsivetwin')
        result, calls = self.run_upload()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ev(tab, 'window.twin()'), {'hidden': [], 'visible': [self.pdf.name]})

    def test_responsive_twin_real_drift_refused(self):
        # The twin elsewhere must not mask the selected slot leaving its form.
        tab = self.tab('responsivedrift', 500)
        result, calls = self.run_upload()
        self.assertEqual(self.ev(tab, "document.querySelector('#host input').form.id"), 'applicationB')
        self.assertFalse(result.ok, result.detail)
        self.assertIn('actual form owner changed', result.detail)
        self.assertEqual(len(calls), 1)

    def test_review_preexisting_same_basename(self):
        tab = self.tab('preexisting')
        result, calls = self.run_upload()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(calls), 1, 'must write requested bytes, not trust a basename')
        self.assertEqual(self.ev(tab, "document.querySelector('[data-testid=input-resume]').files[0].size"), self.pdf.stat().st_size)

    def test_review_counterfeit_same_size(self):
        self.assertEqual(self.pdf.stat().st_size, 36)
        tab = self.tab('counterfeit')
        result, calls = self.run_upload()
        self.assertFalse(result.ok, result.detail)
        self.assertEqual(len(calls), 2)

    def test_review_streaming_http_budget(self):
        stop = threading.Event()
        class Slow(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args): pass
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length', '11'); self.end_headers()
                try:
                    for part in b'[' + b' '*9 + b']':
                        self.wfile.write(bytes([part])); self.wfile.flush()
                        if stop.wait(.95): break
                except (BrokenPipeError, ConnectionResetError): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        start = time.monotonic()
        try:
            result = u.upload_file(f'http://127.0.0.1:{server.server_port}', str(self.pdf))
            elapsed = time.monotonic() - start
            print(f'  streaming HTTP: ok={result.ok} elapsed={elapsed:.3f}s {result.detail}', flush=True)
            self.assertFalse(result.ok)
            self.assertLess(elapsed, u.UPLOAD_BUDGET_SEC + .7)
        finally:
            stop.set(); server.shutdown(); server.server_close(); worker.join(timeout=2)

    def slow_parser(self, delay):
        # Shine's job-apply-flow: the page POSTs the picked file to its resume
        # parser on change and shows the name once that call answers (8.1 s live).
        stop = threading.Event()
        page = (b'<!doctype html><label>Upload Your Resume<input type="file" id="resume" accept=".pdf"></label>'
                b'<p id="shown"></p><script>document.getElementById("resume").onchange=e=>{const f=e.target.files[0];'
                b'fetch("/registration/resume-parser",{method:"POST",body:f}).then(()=>{shown.textContent=f.name})}</script>')
        class Parser(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args): pass
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(page))); self.end_headers(); self.wfile.write(page)
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                if stop.wait(delay): return
                self.send_response(201); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Parser)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        self.addCleanup(lambda: (stop.set(), server.shutdown(), server.server_close(), worker.join(timeout=2)))
        tab = self.http(f'/json/new?http://127.0.0.1:{server.server_port}/', 'PUT'); self.tabs.append(tab)
        deadline = time.monotonic() + 3
        while not self.ev(tab, "!!document.getElementById('resume')"):
            self.assertLess(time.monotonic(), deadline); time.sleep(.02)
        start = time.monotonic()
        result = u.upload_file(self.base, str(self.pdf))
        elapsed = time.monotonic() - start
        print(f'  slow parser {delay}s: ok={result.ok} elapsed={elapsed:.3f}s {result.detail}', flush=True)
        return result, elapsed

    def test_site_upload_call_slower_than_budget(self):
        with patch.object(u, 'UPLOAD_BUDGET_SEC', 2.0), patch.object(u, 'INFLIGHT_GRACE_SEC', 8.0):
            result, elapsed = self.slow_parser(3.0)
        self.assertTrue(result.ok, result.detail)
        self.assertGreater(elapsed, 3.0)

    def test_site_upload_call_hung_is_bounded(self):
        with patch.object(u, 'UPLOAD_BUDGET_SEC', 2.0), patch.object(u, 'INFLIGHT_GRACE_SEC', 4.0):
            result, elapsed = self.slow_parser(60.0)
        self.assertFalse(result.ok, result.detail)
        self.assertIn('in flight', result.detail)
        self.assertGreater(elapsed, 3.5)
        self.assertLess(elapsed, 4.0 + u.FALLBACK_BUDGET_SEC + 1)

    def test_retained_remount(self):
        tab = self.tab('retain'); old = u.find_file_inputs(self.base)[0].backend_node_id
        result, calls = self.run_upload()
        self.assertTrue(result.ok, result.detail)
        self.assertNotEqual(result.node_id, old)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.files(tab), [[self.pdf.name]])

    def _owner_lookup_race(self, *, detach):
        tab = self.tab()
        original = u._form_owner
        evidence = []
        def raced(cdp, backend_node_id):
            if not evidence:
                state = self.ev(tab, """(() => {
                    const old=document.querySelector('[data-testid=input-resume]');
                    if (!old.files.length) return null;
                    const form=old.form;
                    window.raceOld=old;
                    if (DETACH) {
                        const fresh=old.cloneNode(true); fresh.files=old.files;
                        old.replaceWith(fresh);
                    } else old.setAttribute('form','missing-owner');
                    const live=document.querySelector('[data-testid=input-resume]');
                    return {oldConnected:old.isConnected, oldOwnerNull:old.form===null,
                        formConnected:form.isConnected, sameForm:live.form===form,
                        names:Array.from(live.files,f=>f.name)};
                })()""".replace('DETACH', 'true' if detach else 'false'))
                if state is not None:
                    evidence.append(state)
            return original(cdp, backend_node_id)
        with patch.object(u, '_form_owner', raced):
            result, calls = self.run_upload()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0], dict(oldConnected=not detach, oldOwnerNull=True,
            formConnected=True, sameForm=detach, names=[self.pdf.name]))
        self.assertEqual(len(calls), 1)
        return tab, result

    def test_retained_remount_owner_lookup_race(self):
        # Hook after the real scoped DOM walk selected a node, before owner read.
        tab, result = self._owner_lookup_race(detach=True)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self.files(tab), [[self.pdf.name]])
        self.assertEqual(self.files(tab, 'cover'), [[]])

    def test_connected_null_owner_rejected(self):
        _, result = self._owner_lookup_race(detach=False)
        self.assertFalse(result.ok, result.detail)
        self.assertIn('actual form owner changed', result.detail)

    def test_slower_mount(self):
        tab = self.tab('late', 1800)
        result, _ = self.run_upload()
        self.assertTrue(result.ok, result.detail); self.assertEqual(self.files(tab), [[self.pdf.name]])

    def test_missing_initially(self):
        tab = self.tab('missing', 2800)
        result, _ = self.run_upload()
        self.assertTrue(result.ok, result.detail); self.assertEqual(self.files(tab), [[self.pdf.name]])

    def test_never_ready(self):
        self.tab('never')
        result, calls = self.run_upload()
        self.assertFalse(result.ok); self.assertEqual(calls, [])

    def test_ambiguous_replacement(self):
        self.tab('ambiguous', 500)
        result, calls = self.run_upload()
        self.assertFalse(result.ok); self.assertEqual(len(calls), 1)

    def test_form_replacement(self):
        self.tab('formreplace', 500)
        result, calls = self.run_upload()
        self.assertFalse(result.ok); self.assertEqual(len(calls), 1)

    def test_wrong_slot_and_duplicate_tab(self):
        duplicate = self.tab()
        selected = self.tab('wrongslot', 500)
        # Pin initial CDP ordering, not discovery: real browsers don't promise focus order.
        original = u.list_page_targets
        def ordered(url):
            return sorted(original(url), key=lambda t: t['id'] != selected['id'])
        with patch.object(u, 'list_page_targets', ordered): result, calls = self.run_upload()
        self.assertFalse(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.files(duplicate), [[]])

    def test_total_retry_cap(self):
        tab = self.tab('clearalways')
        result, calls = self.run_upload()
        self.assertFalse(result.ok); self.assertEqual(len(calls), 2)
        self.assertEqual(self.files(tab), [[]])

    def test_inaccessible_verification(self):
        self.tab()
        original = u._Cdp.call
        written = False
        def denied(cdp, method, params=None):
            nonlocal written
            if method == 'DOM.setFileInputFiles': written = True
            if written and method == 'Runtime.callFunctionOn' and 'names:' in (params or {}).get('functionDeclaration', ''):
                raise OSError('verification inaccessible')
            return original(cdp, method, params)
        with patch.object(u._Cdp, 'call', denied): result, _ = self.run_upload()
        self.assertFalse(result.ok)

    def test_unresponsive_cdp_budget(self):
        self.tab()
        original = u._Cdp.call
        written = False
        def stalled(cdp, method, params=None):
            nonlocal written
            if method == 'DOM.setFileInputFiles': written = True
            if written and method == 'Runtime.callFunctionOn' and 'names:' in (params or {}).get('functionDeclaration', ''):
                # Real unanswered CDP request, not a fabricated timeout response.
                params = dict(params or {}, functionDeclaration='function(){return new Promise(()=>{});}', awaitPromise=True)
            return original(cdp, method, params)
        with patch.object(u, 'UPLOAD_BUDGET_SEC', .8), patch.object(u._Cdp, 'call', stalled):
            result, calls = self.run_upload()
        self.assertFalse(result.ok); self.assertEqual(len(calls), 1)

    def test_popup_discovery(self):
        popup = self.tab()
        active = self.tab()
        self.ev(active, "document.querySelector('[data-testid=input-resume]').removeAttribute('data-testid')")
        original = u.list_page_targets
        with patch.object(u, 'list_page_targets', lambda url: sorted(original(url), key=lambda t: t['id'] != active['id'])):
            result, _ = self.run_upload()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self.files(popup), [[self.pdf.name]])
        self.assertTrue(all(not x['files'] for x in self.ev(active, 'window.state()')['inputs']))

    def test_ordinary_shapes(self):
        for shape in ('plain', 'hidden', 'shadow', 'iframe'):
            with self.subTest(shape=shape):
                tab = self.tab(shape)
                result, _ = self.run_upload()
                self.assertTrue(result.ok, result.detail)
                self.assertEqual(self.files(tab), [[self.pdf.name]])
                with urllib.request.urlopen(self.base + '/json/close/' + tab['id'], timeout=3) as r: r.read()
                self.tabs.remove(tab)

    def test_cover_purpose(self):
        tab = self.tab()
        result, _ = self.run_upload(want='cover')
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self.files(tab, 'cover'), [[self.pdf.name]])
        self.assertEqual(self.files(tab), [[]])


if __name__ == '__main__':
    names = unittest.defaultTestLoader.getTestCaseNames(Stability)
    if '--review-only' in sys.argv: names = [n for n in names if n.startswith('test_review_')]
    if '--red-only' in sys.argv: names = ['test_delayed_placeholder']
    suite = unittest.TestSuite(Stability(n) for n in names)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not outcome.wasSuccessful())
