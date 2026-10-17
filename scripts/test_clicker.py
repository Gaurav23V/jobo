#!/usr/bin/env python3
"""Real NEED_CLICK regressions, using only disposable local Chromium and HTTP.

Run: .venv/bin/python scripts/test_clicker.py [-v] [TestClicker.test_native]
No live browser, jobs, database, credentials, or agent is involved. Browser
recovery is patched out so even a failing test cannot launch the user profile.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import clicker, form_filler  # noqa: E402

FIXTURE = Path(__file__).with_name("clicker_probe.html")


class TestClicker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.posts = Counter()
        cls.posts_lock = threading.Lock()
        fixture = FIXTURE.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Application submitted</h1>" if self.path == "/done" else fixture)

            def do_POST(self):
                query = parse_qs(urlsplit(self.path).query)
                run = query["run"][0]
                with cls.posts_lock:
                    cls.posts[run] += 1
                # Exercise both a normal async response and one outside observation.
                time.sleep(2.2 if query["case"][0] == "late_submit" else 0.45)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"message": "Application submitted"}).encode())

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.stack.callback(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.stack.callback(cls.thread.join, 5)
        cls.stack.callback(cls.server.shutdown)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        profile = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="jobo-clicker-test-"))
        pw = cls.stack.enter_context(sync_playwright())
        cls.context = pw.chromium.launch_persistent_context(
            profile, headless=True, viewport={"width": 1000, "height": 800},
            args=["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"],
        )
        cls.stack.callback(cls.context.close)
        port = int((Path(profile) / "DevToolsActivePort").read_text().splitlines()[0])
        if port == 9333:
            raise RuntimeError("Refusing forbidden production CDP port")
        cls.cdp_url = f"http://127.0.0.1:{port}"
        cls.page = cls.context.pages[0]
        cls.stack.enter_context(patch.object(form_filler, "_ensure_browser", return_value=(True, "")))
        original_targets = clicker._page_targets

        def isolated_targets(url):
            if url != cls.cdp_url:
                raise AssertionError(f"Non-isolated CDP requested: {url}")
            targets = original_targets(url)
            assert len(targets) == 1, targets
            return targets

        cls.stack.enter_context(patch.object(clicker, "_page_targets", side_effect=isolated_targets))
        print(f"Isolated Chromium CDP: {cls.cdp_url}, fixture: {cls.base}", flush=True)

    def load(self, scenario):
        self.run_id = f"{self.id()}-{time.monotonic_ns()}"
        self.page.goto(f"{self.base}/?case={scenario}&run={self.run_id}")
        self.page.wait_for_function("typeof window.readState === 'function'")

    def act(self, scenario, hint="Required consent"):
        self.load(scenario)
        result = clicker.click(self.cdp_url, hint)
        state = self.page.evaluate("window.readState()")
        self.evidence = f"result={result!r}, state={state!r}"
        print(f"\n{scenario}: {self.evidence}", flush=True)
        return result, state

    def selected(self, scenario, events=1):
        result, state = self.act(scenario)
        self.assertTrue(state["checked"], self.evidence)
        self.assertEqual(state["clicks"], events, self.evidence)
        self.assertEqual(state["trusted"], events, self.evidence)
        self.assertEqual(state["changes"], events, self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)
        self.assertTrue(result.ok, self.evidence)
        msg = result.as_agent_message().lower()
        self.assertIn("continue filling", msg)
        self.assertNotIn("report your verdict", msg)
        return result, state

    def refused(self, scenario, hint="Required consent", reason=""):
        result, state = self.act(scenario, hint)
        self.assertFalse(result.ok, self.evidence)
        self.assertEqual(state["clicks"], 0, self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)
        if reason:
            self.assertIn(reason, result.note.lower(), self.evidence)
        return result, state

    def test_native(self):
        self.selected("native")

    def test_wrapped(self):
        self.selected("wrapped")

    def test_hidden(self):
        self.selected("hidden")

    def test_hidden_wrapped(self):
        self.selected("hidden_wrapped")

    def test_native_labelledby(self):
        self.selected("native_labelledby")

    def test_native_radio(self):
        _, state = self.selected("native_radio")
        self.assertFalse(state["otherChecked"])
        self.assertEqual(state["other"], 0)

    def test_aria(self):
        self.selected("aria")

    def test_aria_radio(self):
        self.selected("aria_radio")

    def test_aria_labelledby(self):
        self.selected("aria_labelledby")

    def test_aria_button(self):
        result, state = self.act("aria_button", "Submit application")
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["trusted"], 1, self.evidence)
        self.assertIn("Application submitted", state["feedback"])
        self.assertTrue(result.ok, self.evidence)

    def test_checked_idempotent(self):
        self.selected("checked", events=0)

    def test_checked_radio_idempotent(self):
        self.selected("checked_radio", events=0)

    def test_checked_aria_idempotent(self):
        self.selected("checked_aria", events=0)

    def test_two_ensure_requests(self):
        self.selected("native")
        result = clicker.click(self.cdp_url, "Required consent")
        state = self.page.evaluate("window.readState()")
        self.assertTrue(result.ok, repr(result))
        self.assertTrue(state["checked"])
        self.assertEqual(state["clicks"], 1, repr(state))
        self.assertEqual(state["changes"], 1, repr(state))

    def test_rerender_revert(self):
        result, state = self.act("revert")
        self.assertFalse(result.ok, self.evidence)
        self.assertFalse(state["checked"], self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["changes"], 1, self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)
        self.assertIn("checked", result.note.lower())

    def test_dropped_trusted_input_falls_back_to_dom_click(self):
        # GHO-23: Avature consent boxes on the form workers never checked under
        # any trusted input. Simulate input that never reaches the page.
        self.load("native")
        cdp = self.context.new_cdp_session(self.page)
        cdp.send("Input.setIgnoreInputEvents", {"ignore": True})
        try:
            result = clicker.click(self.cdp_url, "Required consent")
        finally:
            cdp.send("Input.setIgnoreInputEvents", {"ignore": False})
            cdp.detach()
        state = self.page.evaluate("window.readState()")
        self.evidence = f"result={result!r}, state={state!r}"
        self.assertTrue(result.ok, self.evidence)
        self.assertEqual(result.strategy, "cdp-mouse+dom-click", self.evidence)
        self.assertTrue(state["checked"], self.evidence)
        self.assertEqual((state["clicks"], state["trusted"], state["changes"]), (1, 0, 1), self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)

    def test_page_cancelled_click_is_named(self):
        result, state = self.act("cancel_click")
        self.assertFalse(result.ok, self.evidence)
        self.assertFalse(state["checked"], self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)
        self.assertIn("page cancelled the click", result.note, self.evidence)
        # The click arrived; the page said no. No DOM click on top of that.
        self.assertEqual((state["clicks"], state["trusted"]), (1, 1), self.evidence)
        self.assertEqual(result.strategy, "cdp-mouse", self.evidence)

    def test_remount_preserves_checked(self):
        self.selected("remount_checked")

    def test_snapshot_navigation_never_clicks_new_document(self):
        self.load("silent_submit")
        original = clicker._snapshot
        navigated = False

        def navigate_before_baseline(cdp):
            nonlocal navigated
            if not navigated:
                navigated = True
                old_box = self.page.locator("#submit").bounding_box()
                self.page.goto(f"{self.base}/?case=success&run={self.run_id}-new")
                self.assertEqual(self.page.locator("#submit").bounding_box(), old_box)
            return original(cdp)

        with patch.object(clicker, "_snapshot", navigate_before_baseline):
            result = clicker.click(self.cdp_url, "Submit application")
        state = self.page.evaluate("window.readState()")
        evidence = f"result={result!r}, new_document={state!r}"
        print(f"\nsnapshot navigation: {evidence}", flush=True)
        self.assertEqual(state["clicks"], 0, evidence)
        self.assertEqual(state["submits"], 0, evidence)
        self.assertFalse(result.delivered, evidence)
        self.assertFalse(result.ok, evidence)

    def test_snapshot_mutation_invalidates_original_plan(self):
        mutations = {
            "detached": "s.replaceWith(s.cloneNode(true))",
            "ambiguous": "s.after(s.cloneNode(true))",
            "moved": "s.style.marginLeft = '300px'",
            "changed": "s.textContent = 'Unrelated action'",
        }
        for kind, mutation in mutations.items():
            with self.subTest(kind=kind):
                self.load("silent_submit")
                original = clicker._snapshot
                mutated = False

                def mutate_before_baseline(cdp):
                    nonlocal mutated
                    if not mutated:
                        mutated = True
                        self.page.evaluate("() => { const s = document.querySelector('#submit'); " + mutation + "; }")
                    return original(cdp)

                with patch.object(clicker, "_snapshot", mutate_before_baseline):
                    result = clicker.click(self.cdp_url, "Submit application")
                state = self.page.evaluate("window.readState()")
                evidence = f"{kind}: result={result!r}, state={state!r}"
                self.assertFalse(result.delivered, evidence)
                self.assertFalse(result.ok, evidence)
                self.assertEqual(state["clicks"], 0, evidence)
                self.assertEqual(state["submits"], 0, evidence)

    def test_remount_rejects_replaced_form_and_control_identity(self):
        # Isolate each identity constraint, plus the reviewer's combined case.
        for replace_form, new_id in [(True, "choice"), (True, "unrelated"), (False, "unrelated")]:
            with self.subTest(replace_form=replace_form, new_id=new_id):
                self.load("native")
                self.page.evaluate("""({replace_form, new_id}) => {
                    const form = document.querySelector('#form');
                    form.action = '/original';
                    document.querySelector('#choice').addEventListener('change', () => {
                        const fresh = document.createElement('input');
                        fresh.id = new_id;
                        fresh.type = 'checkbox';
                        fresh.checked = true;
                        if (replace_form) {
                            const replacement = document.createElement('form');
                            replacement.id = form.id;
                            replacement.action = '/unrelated';
                            replacement.append(fresh);
                            form.replaceWith(replacement);
                        } else {
                            document.querySelector('#choice').replaceWith(fresh);
                            form.querySelector('label').remove();
                        }
                        const label = document.createElement('label');
                        label.htmlFor = new_id;
                        label.textContent = 'Required consent';
                        fresh.after(label);
                        window.choice = () => fresh;
                    }, {once: true});
                }""", {"replace_form": replace_form, "new_id": new_id})
                result = clicker.click(self.cdp_url, "Required consent")
                state = self.page.evaluate("window.readState()")
                evidence = f"form_replaced={replace_form}, id={new_id}, result={result!r}, state={state!r}"
                print(f"\nidentity remount: {evidence}", flush=True)
                self.assertTrue(state["checked"], evidence)
                self.assertEqual(state["clicks"], 1, evidence)
                self.assertEqual(state["submits"], 0, evidence)
                self.assertFalse(result.ok, evidence)

    def test_prechecked_still_verified_after_settle(self):
        result, state = self.act("checked_revert")
        self.assertFalse(result.ok, self.evidence)
        self.assertFalse(state["checked"], self.evidence)
        self.assertEqual(state["clicks"], 0, self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)

    def test_disabled(self):
        result, _ = self.refused("disabled")
        self.assertTrue(result.disabled, self.evidence)

    def test_disabled_fieldset(self):
        result, _ = self.refused("disabled_fieldset")
        self.assertTrue(result.disabled, self.evidence)

    def test_disabled_aria(self):
        result, _ = self.refused("disabled_aria")
        self.assertTrue(result.disabled, self.evidence)

    def test_disabled_submit_names_missing(self):
        result, _ = self.refused("disabled_submit", "Submit application")
        self.assertTrue(result.disabled)
        self.assertIn("name", result.note.lower())
        self.assertIn("Fix the cause", result.as_agent_message())

    def test_unknown_hint_never_submits(self):
        self.refused("missing", "Does not exist", "matched")

    def test_ambiguous_exact(self):
        self.refused("ambiguous", reason="ambiguous")

    def test_ambiguous_partial(self):
        self.refused("ambiguous_partial", "Accept", "ambiguous")

    def test_ambiguous_default_submit(self):
        self.refused("ambiguous_submit", "", "ambiguous")

    def test_default_does_not_choose_next(self):
        self.refused("default_next", "")

    def test_default_submit(self):
        _, state = self.act("default_submit", "")
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["trusted"], 1, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)

    def test_async_submit_no_duplicate(self):
        result, state = self.act("async_submit", "Submit application")
        self.page.wait_for_function("window.readState().feedback === 'Application submitted'")
        with self.posts_lock:
            posts = self.posts[self.run_id]
        self.assertEqual(posts, 1, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["trusted"], 1, self.evidence)
        self.assertTrue(result.ok, self.evidence)

    def assert_unknown(self, scenario):
        result, state = self.act(scenario, "Submit application")
        self.assertFalse(result.ok, self.evidence)
        self.assertFalse(result.changed, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertIn("unconfirmed", result.as_agent_message().lower())
        self.assertIn("do not", result.as_agent_message().lower())
        self.assertNotIn("Retry with NEED_CLICK", result.as_agent_message())

    def test_unknown_async_outcome(self):
        self.assert_unknown("silent_submit")

    def test_preexisting_success_is_not_evidence(self):
        self.assert_unknown("prior_success")

    def test_preexisting_error_is_not_evidence(self):
        self.assert_unknown("prior_error")

    def test_new_validation(self):
        result, state = self.act("new_error", "Submit application")
        self.assertFalse(result.ok, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertIn("validation", result.changed.lower())
        self.assertIn("fix", result.as_agent_message().lower())
        self.assertNotIn("report your verdict", result.as_agent_message().lower())

    def test_native_validation(self):
        result, state = self.act("native_validation", "Submit application")
        self.assertFalse(result.ok, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)
        self.assertIn("validation", result.changed.lower(), self.evidence)

    def test_new_success(self):
        result, state = self.act("success", "Submit application")
        self.assertTrue(result.ok, self.evidence)
        self.assertIn("submitted", result.changed.lower())
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)

    def test_navigation(self):
        self.load("navigation")
        result = clicker.click(self.cdp_url, "Submit application")
        self.page.wait_for_url(self.base + "/done")  # Pump Playwright's cached URL events.
        self.assertEqual(self.page.url, self.base + "/done", repr(result))
        self.assertIn("Application submitted", self.page.inner_text("body"))
        self.assertTrue(result.ok, repr(result))
        self.assertEqual(result.after_url, self.page.url)

    def test_overlay_refusal_does_not_mutate_dialog(self):
        result, state = self.refused("overlay", "Submit application")
        self.assertTrue(result.blocked_by, self.evidence)
        self.assertEqual(state["overlayPointerEvents"], "")
        self.assertTrue(self.page.locator("#overlay").is_visible())

    def test_offscreen(self):
        self.selected("offscreen")

    def test_nested_open_shadow(self):
        self.selected("nested_shadow")

    def test_label_link_does_not_navigate(self):
        result, state = self.act("label_link")
        # Either a safe label point or refusal is allowed, but never the link.
        self.assertEqual(state["hash"], "", self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)
        self.assertEqual(state["clicks"], int(result.ok), self.evidence)
        self.assertEqual(state["checked"], result.ok, self.evidence)


    def test_aria_for_label(self):
        self.selected("aria_for")

    def test_aria_wrapped_label(self):
        self.selected("aria_wrapped")

    def test_role_wrapper_and_backing_input_are_one_control(self):
        self.selected("wrapped_role_native")

    def test_native_checkbox_with_button_role_not_toggled_twice(self):
        self.selected("native_button_role")

    def test_hidden_without_activating_label_refused(self):
        self.refused("hidden_unreachable")

    def test_remount_does_not_verify_unrelated_form(self):
        result, state = self.act("wrong_form_remount")
        self.assertTrue(state["checked"], self.evidence)  # An unrelated new control.
        self.assertFalse(result.ok, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["submits"], 0, self.evidence)

    def test_disabled_ancestor(self):
        result, _ = self.refused("disabled_ancestor")
        self.assertTrue(result.disabled, self.evidence)

    def test_default_submit_ignores_disabled_alternative(self):
        _, state = self.act("default_disabled_alternative", "")
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)

    def test_late_server_success_remains_unknown_until_observed(self):
        self.assert_unknown("late_submit")
        self.page.wait_for_function("window.readState().feedback === 'Application submitted'")
        with self.posts_lock:
            self.assertEqual(self.posts[self.run_id], 1)
        self.assertEqual(self.page.evaluate("window.counts.submits"), 1)

    def test_disabled_after_submit_is_not_confirmation(self):
        result, state = self.act("busy_submit", "Submit application")
        self.assertFalse(result.ok, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertIn("disabled", result.changed)
        self.assertIn("unconfirmed", result.as_agent_message().lower())

    def test_removed_after_submit_is_not_confirmation(self):
        result, state = self.act("removed_submit", "Submit application")
        self.assertFalse(result.ok, self.evidence)
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertIn("removed", result.changed)
        self.assertIn("unconfirmed", result.as_agent_message().lower())

    def test_transport_failure_after_release_never_retries(self):
        self.load("silent_submit")
        original = clicker._Cdp.call

        def lose_ack(cdp, method, params=None):
            response = original(cdp, method, params)
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
                raise RuntimeError("test: release acknowledgement lost")
            return response

        with patch.object(clicker._Cdp, "call", lose_ack):
            result = clicker.click(self.cdp_url, "Submit application")
        state = self.page.evaluate("window.readState()")
        self.assertFalse(result.ok, repr(result))
        self.assertEqual(state["clicks"], 1, repr(state))
        self.assertEqual(state["submits"], 1, repr(state))
        self.assertIn("unconfirmed", result.as_agent_message().lower())
        self.assertNotIn("Retry with NEED_CLICK", result.as_agent_message())

    def test_hover_can_reveal_blocking_dialog(self):
        result, state = self.refused("hover_overlay", "Submit application")
        self.assertTrue(result.blocked_by, self.evidence)
        self.assertEqual(state["overlayPointerEvents"], "")
        self.assertTrue(self.page.locator("#overlay").is_visible())

    def test_partial_overlay_uses_unobstructed_point_without_mutation(self):
        _, state = self.act("partial_overlay", "Submit application")
        self.assertEqual(state["clicks"], 1, self.evidence)
        self.assertEqual(state["trusted"], 1, self.evidence)
        self.assertEqual(state["submits"], 1, self.evidence)
        self.assertEqual(state["overlayPointerEvents"], "")

    def test_legacy_overlay_fixture_is_refused_not_bypassed(self):
        # Its empty div has no semantic proof that bypassing it is safe.
        self.page.goto(FIXTURE.with_name("react_widget_probe.html").as_uri())
        result = clicker.click(self.cdp_url, "Submit application")
        state = json.loads(self.page.evaluate("window.__probeState()"))
        self.assertFalse(result.ok, repr(result))
        self.assertFalse(state["submitted"], repr(state))
        self.assertTrue(result.blocked_by, repr(result))
        self.assertEqual(self.page.locator("#overlay").evaluate("e => e.style.pointerEvents"), "")

    def test_legacy_disabled_finish_explains_missing_phone(self):
        self.page.goto(FIXTURE.with_name("react_widget_probe.html").as_uri())
        result = clicker.click(self.cdp_url, "Finish")
        self.assertFalse(result.ok, repr(result))
        self.assertTrue(result.disabled, repr(result))
        self.assertIn("Phone Number", result.note)
        self.assertIn("Fix the cause", result.as_agent_message())

    def test_first_target_only_never_scans_another_tab(self):
        self.load("missing")
        first = clicker._page_targets(self.cdp_url)[0]
        other = self.context.new_page()
        try:
            other.goto(f"{self.base}/?case=native&run=other")
            session = self.context.new_cdp_session(other)
            target_id = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            session.detach()
            second = {"webSocketDebuggerUrl": self.cdp_url.replace("http:", "ws:") + f"/devtools/page/{target_id}"}
            with patch.object(clicker, "_page_targets", return_value=[first, second]):
                result = clicker.click(self.cdp_url, "Required consent")
            self.assertFalse(result.ok, repr(result))
            self.assertEqual(self.page.evaluate("window.counts.clicks"), 0)
            self.assertEqual(self.page.evaluate("window.counts.submits"), 0)
            self.assertFalse(other.evaluate("window.readState().checked"))
            self.assertEqual(other.evaluate("window.counts.clicks"), 0)
        finally:
            other.close()


class TestDirective(unittest.TestCase):
    def test_parse(self):
        for text, expected in [("NEED_CLICK: Submit application", "Submit application"),
                               ("NEED_CLICK", ""), ("NEED_CLICK:", ""),
                               ("nothing", None)]:
            with self.subTest(text=text):
                self.assertEqual(clicker.parse_request(text), expected)


    def test_blank_directive_never_consumes_next_line(self):
        self.assertEqual(clicker.parse_request("NEED_CLICK\nDo not use as label"), "")
        self.assertEqual(clicker.parse_request("NEED_CLICK:\nNext line"), "")

    def test_prompt_has_scoped_consistent_handoff(self):
        from module4.prompts import SYSTEM_PROMPT

        self.assertIn("DEFAULT for clicking", SYSTEM_PROMPT)
        self.assertIn("not for every navigation", SYSTEM_PROMPT)
        self.assertIn("at most 5 NEED_CLICK", SYSTEM_PROMPT)
        self.assertIn("ENSURE CHECKED", SYSTEM_PROMPT)
        self.assertIn("continue filling", SYSTEM_PROMPT)
        self.assertIn("UNCONFIRMED", SYSTEM_PROMPT)
        self.assertNotIn("clears any overlay", SYSTEM_PROMPT)
        self.assertNotIn("Submit, and every checkbox", SYSTEM_PROMPT)


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    unittest.main(verbosity=2)
