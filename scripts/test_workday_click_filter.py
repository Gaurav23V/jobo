#!/usr/bin/env python3
"""Workday click_filter overlays: NEED_CLICK must click the overlay, once.

GHO-14 (Willowby 1464, Adatum 1513, 2026-10-11): "Create Account",
"Sign In" and "Forgot your password?" all "registered as successful clicks but
produced no visible state change". Measured on a scratch Chromium against the
live willowby.wd1.myworkdayjobs.com Create Account form (2026-10-15, no
account created):

  * Every Workday action button is a pair inside one wrapper:
      <div role=button aria-label="Create Account" data-automation-id=click_filter
           style="position:absolute; z-index:3"></div>
      <button type=submit aria-hidden=true tabindex=-2>Create Account</button>
    `button.click()` does nothing; a click on the overlay runs the form.
  * After typing the passwords the page sits so the consent box is just in
    view and Create Account's centre is ~590 px down: below the fold of the
    ~585 px jobo viewport. agent-browser's `click @ref` (pre click_scroller,
    i.e. both failed runs) pressed there and hit nothing. Reproduced: no-scroll
    click 0 effect; scrollintoview + click -> Workday validation fired.
  * NEED_CLICK, the trusted fallback, refused the button outright: the overlay
    and its hidden twin are both "Create Account", so the clicker answered
    "ambiguous control hint; no click sent". The same pair holds the final
    Review-page "Submit", so a Workday submit could never go through NEED_CLICK.

Self-isolated: own headless Chromium on a random port (never 9333), inline
fixture, no network, no live browser, jobs, DB or agent.
Run: bash scripts/run_regression_tests.sh test_workday_click_filter
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import clicker, form_filler  # noqa: E402

# Mirrors the measured DOM: overlay first, hidden native button second, same
# wrapper; the overlay is transparent and absolutely positioned over it.
PAIR = """
<div class="wrap">
  <div role="button" tabindex="0" aria-label="{label}" data-automation-id="click_filter"
       class="filter" data-for="{label}"></div>
  <button type="submit" tabindex="-2" aria-hidden="true" class="native"
          data-automation-id="{auto}">{label}</button>
</div>"""

PAGE = """<!doctype html><html><head><style>
  body {{ margin: 0; font: 14px sans-serif; }}
  .spacer {{ height: 1400px; }}
  .wrap {{ position: relative; width: 240px; height: 40px; margin: 8px; }}
  .native {{ width: 240px; height: 40px; }}
  .filter {{ position: absolute; inset: 0; z-index: 3; cursor: pointer; }}
</style></head><body>
<header><button data-automation-id="utilityButtonSignIn">Sign In</button></header>
<div class="spacer"></div>
<form id="f" onsubmit="event.preventDefault(); window.log.push('native-submit')">
  <input type="email" aria-label="Email Address">
  {body}
</form>
<script>
  window.log = [];
  document.addEventListener('click', e => {{
    const f = e.target.closest('[data-automation-id=click_filter]');
    if (f) {{
      window.log.push('filter:' + f.dataset.for + ':' + (e.isTrusted ? 'trusted' : 'synthetic'));
      if (f.dataset.for === 'Submit') {{
        const h = document.createElement('h2');
        h.textContent = 'Application submitted';
        document.body.append(h);
      }}
    }} else if (e.target.closest('button')) {{
      window.log.push('button:' + e.target.closest('button').textContent.trim());
    }}
  }}, true);
</script></body></html>"""


def pair(label: str, auto: str) -> str:
    return PAIR.format(label=label, auto=auto)


class TestWorkdayClickFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        profile = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="jobo-wd-filter-"))
        pw = cls.stack.enter_context(sync_playwright())
        # Short viewport like the jobo Chromium (~585 px): every control below
        # sits under the fold until the clicker scrolls it into view.
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

    def act(self, body: str, hint: str):
        self.page.set_content(PAGE.format(body=body))
        self.page.evaluate("window.scrollTo(0, 0)")
        result = clicker.click(self.cdp_url, hint)
        log = self.page.evaluate("window.log")
        self.evidence = f"result={result!r}, log={log!r}"
        print(f"\n{hint or '<blank>'}: {self.evidence}", flush=True)
        return result, log

    def create_account_form(self) -> str:
        return (pair("Create Account", "createAccountSubmitButton")
                + '<button type="button" data-automation-id="signInLink">Sign In</button>'
                + '<button type="button" data-automation-id="forgotPasswordLink">'
                  'Forgot your password?</button>')

    def test_create_account_clicks_overlay_once(self):
        result, log = self.act(self.create_account_form(), "Create Account")
        self.assertTrue(result.delivered, self.evidence)
        self.assertEqual(result.strategy, "cdp-mouse", self.evidence)
        self.assertEqual(result.matched, "Create Account", self.evidence)
        self.assertEqual(log, ["filter:Create Account:trusted"], self.evidence)

    def test_hidden_twin_is_not_a_second_candidate(self):
        result, _ = self.act(self.create_account_form(), "No such control")
        self.assertFalse(result.delivered, self.evidence)
        self.assertEqual(result.candidates.count("Create Account"), 1, self.evidence)

    def test_review_submit_pair_blank_need_click(self):
        # Final Review page: the blank NEED_CLICK must resolve the Submit pair
        # to its overlay, click once and see the confirmation.
        result, log = self.act(pair("Submit", "bottom-navigation-next-button"), "")
        self.assertTrue(result.ok, self.evidence)
        self.assertEqual(result.outcome, "confirmation", self.evidence)
        self.assertEqual(log, ["filter:Submit:trusted"], self.evidence)

    def test_sign_in_link_below_fold(self):
        # Header "Sign In" plus the form's "Sign In" link stay two real,
        # distinct controls: still ambiguous, nothing clicked.
        result, log = self.act(self.create_account_form(), "Sign In")
        self.assertFalse(result.delivered, self.evidence)
        self.assertIn("ambiguous", result.note, self.evidence)
        self.assertEqual(log, [], self.evidence)
        result, log = self.act(self.create_account_form(), "Forgot your password?")
        self.assertTrue(result.delivered, self.evidence)
        self.assertEqual(log, ["button:Forgot your password?"], self.evidence)

    def test_two_visible_same_label_buttons_stay_ambiguous(self):
        body = ('<div class="wrap"><button type="button">Create Account</button>'
                '<button type="button">Create Account</button></div>')
        result, log = self.act(body, "Create Account")
        self.assertFalse(result.delivered, self.evidence)
        self.assertIn("ambiguous", result.note, self.evidence)
        self.assertEqual(log, [], self.evidence)

    def test_lone_aria_hidden_button_still_clickable(self):
        # Only a hidden twin of a visible same-label sibling is dropped.
        body = '<button type="button" aria-hidden="true">Continue</button>'
        result, log = self.act(body, "Continue")
        self.assertTrue(result.delivered, self.evidence)
        self.assertEqual(log, ["button:Continue"], self.evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
