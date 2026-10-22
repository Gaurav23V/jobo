#!/usr/bin/env python3
"""Greenhouse forms that got stuck in the 2026-10-22 night run (GHO-71).

1. Stripe 2268 and Pantheon 2107 embed their Greenhouse form in a cross-origin
   iframe. NEED_FIELDS listed the outer page only (one control, a locale
   picker), the agents hand-drove the iframe with raw CDP and ran out of
   steps. The first NEED_UPLOAD / NEED_FIELDS now opens the iframe's form in
   the tab (module4/embedded_forms.py) and says so.
2. Abnormal 2119, Coinbase 2022 and Nexla 516: input#resume held our file but
   Greenhouse never took it (no S3 POST, no file chip) and Submit said
   "Resume/CV is required.". 516 went through after one page-dispatched
   change event. The uploader now re-sends change once when the site showed
   no reaction, and only then; a site that reacted gets exactly one event.
   Greenhouse's direct S3 POST also counts as the site's reaction now.
3. Greenhouse's phone Country lists "India +91" and then shows "+91": the
   setter reported a correct pick as "did not survive" (2022, 2119).
4. jobo run-report filed 2268 as personal:disclosures (needs_g) because its
   reason named the "voluntary disclosures" it never reached.

Self-isolated: its own headless Chromium on a random CDP port and a local
HTTP server (iframe from localhost inside a page from 127.0.0.1).
Run: uv run python scripts/test_greenhouse_embed.py
"""
from __future__ import annotations

import functools
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4 import agent_driver, embedded_forms, form_filler, run_report, uploader  # noqa: E402

RESUME = REPO / "personal.example" / "resume.pdf"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail[:300]}]" if detail and not ok else ""), flush=True)
    if not ok:
        FAILS.append(name)


def unit_checks() -> None:
    gh = "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=8301253&validityToken=abc"
    check("careers page + Greenhouse embed iframe -> the iframe URL",
          embedded_forms.embedded_form_url("https://stripe.com/jobs/listing/x/8301253",
                                           ["https://js.stripe.com/v3/m.html", gh]) == gh)
    check("Ashby embed iframe is promoted too",
          embedded_forms.embedded_form_url("https://partech.com/careers-at-par/?ashby_jid=1c",
                                           ["https://jobs.ashbyhq.com/par/1c/application?embed=js"])
          == "https://jobs.ashbyhq.com/par/1c/application?embed=js")
    check("a page already on Greenhouse is never promoted",
          embedded_forms.embedded_form_url("https://job-boards.greenhouse.io/coinbase/jobs/1", [gh]) is None)
    check("unrelated iframes (captcha, video) are ignored",
          embedded_forms.embedded_form_url("https://acme.com/careers",
                                           ["https://www.google.com/recaptcha/api2/anchor?k=1",
                                            "https://www.youtube.com/embed/xyz"]) is None)

    reasons = {
        "2268": ("Stripe", "stuck:Stripe's Greenhouse application form is a cross-origin iframe that Hermes's "
                 "field-lister/snapshot cannot see into (NEED_FIELDS only returns the outer page's locale "
                 "picker); had to hand-drive via raw CDP keyboard events, got Name/Email/Phone/Location set and "
                 "both files uploaded, but the School combobox stopped opening its options listbox and several "
                 "required dropdown questions (education degree, work authorization, voluntary disclosures) "
                 "were never reached before the step budget ran out."),
        "2107": ("Pantheon", "stuck:Greenhouse embedded form's accessibility tree stopped exposing iframe content "
                 "to browser_snapshot/NEED_FIELDS after a cookie-consent interaction, forcing raw CDP DOM access"),
    }
    for jid, (company, reason) in reasons.items():
        sig = run_report.signature_for(reason, company)
        check(f"run-report files {jid} as form:embedded_iframe (got {sig})", sig == "form:embedded_iframe")
    check("a captcha in its own iframe stays a captcha",
          run_report.signature_for("captcha_failed:hCaptcha visual challenge (rendered inside a cross-origin "
                                   "enclave iframe on jobs.lever.co)", "X").startswith("captcha:"))
    check("a real disclosure question still needs G",
          run_report.signature_for("stuck:required conflict of interest disclosures question", "X")
          == "personal:disclosures")


def _serve() -> tuple[http.server.ThreadingHTTPServer, int]:
    scripts = REPO / "scripts"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            if path.startswith("/embed/job_app"):
                return str(scripts / "greenhouse_embed_form.html")
            return super().translate_path(path)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                          functools.partial(Handler, directory=str(scripts)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _page(cdp: str) -> dict:
    return [t for t in json.load(urllib.request.urlopen(f"{cdp}/json/list")) if t["type"] == "page"][0]


def _goto(cdp: str, url: str) -> None:
    with form_filler._Cdp(_page(cdp)["webSocketDebuggerUrl"]) as c:
        c.call("Page.navigate", {"url": url})
    time.sleep(1.0)


def _eval(cdp: str, expr: str):
    with form_filler._Cdp(_page(cdp)["webSocketDebuggerUrl"]) as c:
        r = c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r["result"]["result"].get("value")


def browser_checks() -> None:
    srv, port = _serve()
    with tempfile.TemporaryDirectory(prefix="jobo-gho71-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--site-per-process", "--window-size=1280,900",
             "--user-data-dir=" + profile, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = Path(profile) / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            form = f"http://127.0.0.1:{port}/embed/job_app?for=acme&token=1&mode="

            # 2. Upload: re-send change once, only when the site did not react.
            for mode, want_changes, want_committed, resent in (
                    ("eager", 1, True, False), ("late", 2, True, True), ("held", 2, False, True)):
                _goto(cdp, form + mode)
                r = uploader.upload_file(cdp, str(RESUME), want="resume")
                changes = _eval(cdp, "window.__changes")
                committed = bool(_eval(cdp, "window.__committed"))
                check(f"upload ({mode}): ok, {want_changes} change event(s), committed={want_committed}",
                      r.ok and changes == want_changes and committed == want_committed
                      and (("re-sent" in r.detail) == resent),
                      f"ok={r.ok} changes={changes} committed={committed} detail={r.detail}")
            _goto(cdp, form + "late")
            uploader.upload_file(cdp, str(RESUME), want="resume")
            _eval(cdp, "document.getElementById('submit').click()")
            check("late-listening form: Submit no longer says Resume/CV is required",
                  _eval(cdp, "document.getElementById('errors').textContent") == "")

            # 3. Phone country: "India +91" shown as "+91" is a correct pick.
            _goto(cdp, form + "eager")
            ls = form_filler.list_fields(cdp)
            form_filler.remember_listing(cdp, ls)
            idx = next((f.index for f in ls.fields if f.label.lower().startswith("country")), None)
            res = form_filler.set_field(cdp, "", "India", index=idx)
            check("Country = India on a dial-code picker that shows +91 is FIELD_SET",
                  res.ok and res.readback == "+91", res.as_agent_message())

            # 1. Embedded form: the first NEED_FIELDS opens the iframe in the tab.
            _goto(cdp, f"http://127.0.0.1:{port}/greenhouse_embed_outer.html")
            time.sleep(1.0)
            outer_only = form_filler.list_fields(cdp)
            check("outer careers page alone lists no form fields (the agents' view)",
                  not any(f.label.lower().startswith("first name") for f in outer_only.fields))
            reply = agent_driver._resolve_fields_listing(cdp_url=cdp, job_id=99971)
            url = _page(cdp)["url"]
            check("NEED_FIELDS opens the embedded form in the tab and says so",
                  reply.startswith("[jobo: this careers page embeds") and "/embed/job_app" in url
                  and "First Name" in reply, reply[:400])
            again = agent_driver._resolve_fields_listing(cdp_url=cdp, job_id=99971)
            check("a second NEED_FIELDS on the opened form does not navigate again",
                  not again.startswith("[jobo:") and _page(cdp)["url"] == url, again[:200])
            _goto(cdp, f"http://127.0.0.1:{port}/greenhouse_embed_outer.html")
            time.sleep(1.0)
            up = agent_driver._resolve_upload("NEED_UPLOAD: resume", cdp_url=cdp, resume_path=str(RESUME),
                                              cover_path=None, job_id=99971)
            check("NEED_UPLOAD on the careers page opens the form, then uploads there",
                  up.startswith("[jobo: this careers page embeds") and "UPLOAD_DONE" in up
                  and bool(_eval(cdp, "window.__committed")), up[:400])
        finally:
            proc.kill()
            srv.shutdown()


def main() -> int:
    unit_checks()
    browser_checks()
    print(f"\n{'FAIL' if FAILS else 'PASS'} test_greenhouse_embed: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
