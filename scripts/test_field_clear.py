#!/usr/bin/env python3
"""NEED_FIELD line boundaries and real, disposable-browser plaintext clearing.

Run: .venv/bin/python -B scripts/test_field_clear.py
No application, credentials, production CDP, or browser recovery is used.
"""
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import agent_driver, form_filler as f
from playwright.sync_api import sync_playwright


class Protocol(unittest.TestCase):
    def test_empty_line_never_consumes_diagnostics(self):
        for ending in ('\n', '\r\n'):
            for suffix in ('⚠ A previous `hermes update` pulled new code but did not restart running gateways.',
                           'STATUS: stuck', 'session_id: original-id'):
                text = 'NEED_FIELD: #6 = ' + ending * 2 + suffix + ending + 'session_id: original-id'
                with self.subTest(ending=repr(ending), suffix=suffix):
                    self.assertEqual(f.parse_request(text), ('#6', '', 6))
                    match = agent_driver.FIELD_REQUEST_RE.search(text)
                    assert match is not None
                    self.assertEqual(f.parse_request(match.group()), ('#6', '', 6))
                    self.assertNotIn(suffix, match.group())

    def test_ordinary_literals_and_quoted_empty(self):
        cases = [
            ('NEED_FIELD: #6 = ""', ('#6', '', 6)),
            ("NEED_FIELD: #6 = ''", ('#6', '', 6)),
            (' \tneed_field:\t#6\t=\t\r\n', ('#6', '', 6)),
            ('NEED_FIELD: College Name = LIIT Springfield', ('College Name', 'LIIT Springfield', None)),
            ('NEED_FIELD: 3=India', ('3', 'India', 3)),
            ('NEED_FIELD: # 6=India', ('# 6', 'India', 6)),
            ('NEED_FIELD: #0006="India"', ('#0006', '"India"', None)),
            ('NEED_FIELD: #6 = " "', ('#6', '" "', 6)),
            ("NEED_FIELD: #6 = 'Acme'", ('#6', "'Acme'", 6)),
            ('NEED_FIELD: #6 = a=b #x ""', ('#6', 'a=b #x ""', 6)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(f.parse_request(text), expected)
                match = agent_driver.FIELD_REQUEST_RE.search(text)
                assert match is not None
                self.assertEqual(f.parse_request(match.group()), expected)

    def test_stderr_diagnostics_and_two_tuple_are_preserved(self):
        warning = '⚠ A previous `hermes update` pulled new code but did not restart running gateways.'
        completed = subprocess.CompletedProcess([], 1, 'NEED_FIELD: #6 = \n', warning + '\nsession_id: original-id')
        with patch.object(agent_driver, '_hermes_bin', return_value='not-executed'), \
             patch.object(agent_driver, '_env_for_agent', return_value={}), \
             patch.object(agent_driver.subprocess, 'run', return_value=completed):
            output, timed_out = agent_driver._run_hermes(
                prompt='fixture', cdp_url='http://unused', timeout_sec=1, max_turns='1')
        self.assertFalse(timed_out)
        self.assertIn(warning, output)
        self.assertIn('session_id: original-id', output)
        match = agent_driver.FIELD_REQUEST_RE.search(output)
        assert match is not None
        self.assertEqual(f.parse_request(match.group()), ('#6', '', 6))

    def test_no_multiline_or_empty_hint(self):
        for text in ('NEED_FIELD: \n#6 = India', 'NEED_FIELD: #6\n= India',
                     'NEED_FIELD: \t= India', 'NEED_FIELD: =',
                     'NEED_FIELD:\r\n#6 = India', 'NEED_FIELD: #\n6 = India',
                     'NEED_FIELDS', 'NEED_FIELD: no equals'):
            with self.subTest(text=text):
                self.assertIsNone(f.parse_request(text))
                self.assertIsNone(agent_driver.FIELD_REQUEST_RE.search(text))


class PlainClear(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='jobo-field-clear-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.context = cls.pw.chromium.launch_persistent_context(
            cls.tmp.name, executable_path='/usr/bin/chromium', headless=True,
            args=['--no-sandbox', '--remote-debugging-port=0',
                  '--disable-background-networking', '--disable-component-update',
                  '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1'])
        cls.addClassCleanup(cls.context.close)
        port = (Path(cls.tmp.name) / 'DevToolsActivePort').read_text().splitlines()[0]
        assert port != '9333'
        cls.cdp = 'http://127.0.0.1:' + port
        cls.context.route('**/*', lambda route: route.abort())
        cls.page = cls.context.pages[0]
        cls.guard = patch.object(f, '_ensure_browser', return_value=(True, 'isolated: no recovery'))
        cls.guard.start()
        cls.addClassCleanup(cls.guard.stop)
        print('Isolated CDP:', cls.cdp, flush=True)

    def fixture(self, mode='accept', tag='input'):
        control = '<textarea id="company">Old Company</textarea>' if tag == 'textarea' else '<input id="company" value="Old Company">'
        self.page.set_content('<input>' * 5 + '<label for="company">Current Company</label>' + control)
        self.page.evaluate('''mode => {
            const el=document.querySelector('#company'); window.original=el;
            const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
            const desc=Object.getOwnPropertyDescriptor(proto,'value');
            let tracked=el.value;window.held=el.value;window.events=[];
            // React-like own value tracker: native prototype setter must bypass it.
            Object.defineProperty(el,'value',{get(){return desc.get.call(this)},
                set(v){tracked=v;desc.set.call(this,v)}});
            el.addEventListener('input',()=>{
                events.push(el.value);
                if(el.value!==tracked){tracked=el.value;held=el.value}
                setTimeout(()=>{
                    if(mode==='detach')el.remove();
                    else if(mode==='replace')el.replaceWith(el.cloneNode(true));
                    else if(mode==='retarget')el.name='different-field';
                    else if(mode==='revert')desc.set.call(el,'Old Company');
                    else if(mode==='revert-empty')desc.set.call(el,'');
                    else desc.set.call(el,held);
                },80);
            });
        }''', mode)
        listing = f.list_fields(self.cdp)
        self.assertTrue(listing.ok)
        f.remember_listing(self.cdp, listing)

    def request(self, value):
        match = agent_driver.FIELD_REQUEST_RE.search('NEED_FIELD: #6 = ' + value + '\n\n⚠ A previous `hermes update` pulled new code but did not restart running gateways.\nsession_id: fixture')
        assert match is not None
        parsed = f.parse_request(match.group())
        assert parsed is not None
        hint, value, index = parsed
        return f.set_field(self.cdp, hint, value, index=index)

    def test_blank_and_quoted_empty_are_committed_empty_not_quotes(self):
        for literal in ('', '""', "''"):
            for tag in ('input', 'textarea'):
                with self.subTest(literal=literal, tag=tag):
                    self.fixture(tag=tag)
                    result = self.request(literal)
                    state = self.page.evaluate('({value:original.value,held,events,connected:original.isConnected})')
                    self.assertEqual(state, dict(value='', held='', events=[''], connected=True))
                    self.assertTrue(result.ok, result.note)
                    self.assertEqual(result.readback, '')
                    self.assertFalse(result.reverted)
                    self.assertTrue(self.request(literal).ok, 'already empty is idempotent')

    def test_direct_blank_set_reports_verified_clear(self):
        self.fixture()
        result = f.set_field(self.cdp, '#6', '', index=6)
        self.assertEqual(self.page.locator('#company').input_value(), '')
        self.assertEqual(self.page.evaluate('held'), '')
        self.assertTrue(result.ok, result.note)

    def test_empty_must_survive_on_same_connected_node(self):
        for mode in ('detach', 'replace', 'retarget', 'revert'):
            with self.subTest(mode=mode):
                self.fixture(mode)
                result = self.request('')
                self.assertFalse(result.ok, result)

    def test_nonempty_revert_still_fails_and_quotes_are_literal(self):
        self.fixture('revert-empty')
        self.assertFalse(self.request('New Company').ok)
        self.fixture()
        self.assertTrue(self.request('"Acme"').ok)
        self.assertEqual(self.page.locator('#company').input_value(), '"Acme"')

    def test_missing_field_and_select_options_never_gain_empty_success(self):
        for markup, index in (('', 6), ('<select id="empty"></select>', 1),
                              ('<select id="empty"><option value=""></option></select>', 1),
                              ('<input role="combobox" aria-label="Empty options">', 1)):
            with self.subTest(markup=markup):
                self.page.set_content(markup)
                listing = f.list_fields(self.cdp)
                f.remember_listing(self.cdp, listing)
                self.assertFalse(f.set_field(self.cdp, '#' + str(index), '', index=index).ok)


if __name__ == '__main__':
    unittest.main(verbosity=2)
