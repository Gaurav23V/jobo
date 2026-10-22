#!/usr/bin/env python3
"""Company-site resume uploads that failed in the 2026-10-15 14:29 round (GHO-19).

  * Myrtleby 1635 (TurboHire): two hidden inputs with identical attributes,
    one beside an "Autofill With Resume" button, one inside div#drop-resume.
    Every request, even the agent's correct `inside div#drop-resume`, was
    refused as "ambiguous": the tie-break read neither the button nor
    ancestor ids, and `[type=file]` in a hint matched both inputs. The reply
    also began "no <input type=file> found" while listing two inputs.
  * Tailspin 1745 (Eightfold): measured on jobs.tailspin.com. On change the
    widget remounts an empty input and its beforeUpload awaits a role=dialog
    "Data Privacy Agreement"; nothing is POSTed until "I Agree". The uploader
    read "upload input disappeared after the write" 4 times. Clicking I Agree
    POSTed /api/application/v2/resume_upload (HTTP 200) and showed the file.
    Live (19:18 run, GHO-26) the same dialog was never found on 1745/1757:
    it fades in by CSS transitions, and on the form workers' hidden pages
    animations never advance, so it sat open at visibility:hidden.
  * Kulfi 1999 (Dover): two byte-identical hidden inputs in one form, an
    "Autofill from resume" dropzone and the required "Resume *" field. The
    tie-break read neither the dropzone's own text (the input's parent is
    the role=button) nor the plain-div label, so the default request and all
    four of the agent's hints (`under "Resume"`, `under "Resume *" heading
    ..., not the "Autofill from resume" one`, the dropzone text) were refused
    as ambiguous until the job's upload cap ran out (GHO-74).
  * Heron 1648: the agent asked for an upload on a Workday start page whose
    "Autofill with Resume" click a cookie banner had swallowed. There was no
    input; the job then stopped on Workday account creation, not on upload.

Page: scripts/upload_company_sites_probe.html. Self-isolated: own headless
Chromium on a random port (never 9333).
Run: bash scripts/run_regression_tests.sh test_upload_company_sites
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import uploader  # noqa: E402
from module4.agent_driver import NO_FILE_INPUT  # noqa: E402
from scripts.test_upload_stability import Stability  # noqa: E402

PROBE = Path(__file__).resolve().parent / "upload_company_sites_probe.html"
CDP = ""
RESUME: Path
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:66s} {note}")


def state(tab: dict):
    with uploader._Cdp(tab["webSocketDebuggerUrl"]) as c:
        r = c.call("Runtime.evaluate", {"expression": "window.__state && window.__state()", "returnByValue": True})
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else None


def open_probe(query: str) -> dict:
    url = PROBE.as_uri() + "?" + query
    # Quote the page URL: /json/new would read its `&` as its own separator.
    req = urllib.request.Request(f"{CDP}/json/new?{urllib.parse.quote(url, safe=':/?=')}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.load(r)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if state(tab) is not None:
                break
        except Exception:
            pass
        time.sleep(0.05)
    return tab


def run(query: str, hint: str = "", *, hidden: bool = False):
    """`hidden`: a blank tab opened in front leaves the probe page hidden, as
    a form-worker page is when its headed window is off screen (GHO-26)."""
    tab = open_probe(query)
    cover = None
    if hidden:
        req = urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            cover = json.load(r)
    try:
        res = uploader.upload_file(CDP, str(RESUME), want="resume", hint=hint)
        time.sleep(0.5)
        st = state(tab)
        assert st is not None, "probe state unreadable"
        return res, st
    finally:
        for t in (tab, cover):
            try:
                if t:
                    urllib.request.urlopen(f"{CDP}/json/close/{t['id']}", timeout=5).read()
            except Exception:
                pass


def _checks() -> int:
    name = RESUME.name

    print("Myrtleby / TurboHire: Autofill dropzone vs div#drop-resume")
    for hint in ("", "inside div#drop-resume", 'input#drop-resume input[type=file], under "Upload Resume *"'):
        res, st = run("site=turbohire", hint)
        check(f"hint {hint[:40]!r} -> the Upload Resume input, not Autofill",
              res.ok and st["uploads"] == {"resume": 1}, f"ok={res.ok} uploads={st['uploads']} {res.detail[:60]}")

    print("\nKulfi / Dover: Autofill-from-resume dropzone vs the Resume * field")
    for hint in ("", 'under "Resume" (Drag and drop file or browse computer)',
                 'under "Resume *" heading (Drag and drop file or browse computer), '
                 'not the "Autofill from resume" one',
                 'inside button labeled "Drag and drop file or browse computer (PDF, 5 MB maximum)"',
                 'second file input, parent text "Drag and drop file or browse computer(PDF, 5 MB maximum)"'):
        res, st = run("site=dover", hint)
        check(f"hint {hint[:40]!r} -> the Resume * input, not Autofill",
              res.ok and st["uploads"] == {"resume": 1}, f"ok={res.ok} uploads={st['uploads']} {res.detail[:60]}")
    res, st = run("site=dover", 'the "Autofill from resume" zone')
    check("the agent can still ask for the autofill zone by name",
          res.ok and st["uploads"] == {"autofill": 1}, f"ok={res.ok} uploads={st['uploads']}")
    tab = open_probe("site=dover")
    try:
        names = [t.descriptor for t in uploader.find_file_inputs(CDP)]
    finally:
        urllib.request.urlopen(f"{CDP}/json/close/{tab['id']}", timeout=5).read()
    check("candidates name the autofill zone and the Resume * field",
          sorted(names) == ['accept=".pdf" (autofill zone)', 'accept=".pdf" under "Resume *"'], str(names))

    print("\nHint parsing")
    t = uploader.UploadTarget(1, {"type": "file", "accept": ".pdf"}, "t", "ws",
                              context="drop-resume jss1080 mui-box __rendered__")
    check("`[type=file]` alone picks out nothing", not uploader._hint_matches(t, "input[type=file]"))
    check("`#drop-resume` matches an ancestor id", uploader._hint_matches(t, "div#drop-resume"))
    check("an unrelated `#id` does not", not uploader._hint_matches(t, "div#drop-autofill"))

    print("\nTwo inputs nothing tells apart")
    res, st = run("site=twins")
    check("still refused as ambiguous", not res.ok and "ambiguous" in res.detail, res.detail[:80])
    check(f"reply no longer says {NO_FILE_INPUT!r} while listing inputs",
          NO_FILE_INPUT not in res.detail and len(res.candidates) == 2, res.detail[:80])

    print("\nTailspin / Eightfold: upload waits on a Data Privacy Agreement dialog")
    res, st = run("site=eightfold")
    check("upload succeeds after the consent dialog is accepted",
          res.ok and st["uploads"] == {"resume": 1} and st["chips"] == [name],
          f"ok={res.ok} uploads={st['uploads']} chips={st['chips']}")
    check("only I Agree was clicked (not the side panel's Accept, not Cancel)",
          st["clicks"] == ["agree"], f"clicks={st['clicks']}")
    check("the optional marketing checkbox was left alone", st["marketing"] is False)
    check("the agent is told the dialog was accepted",
          'accepted the site\'s "Data Privacy Agreement" dialog' in res.detail, res.detail[-110:])

    print("\nEightfold on a hidden page (live 19:18 run, 1745/1757: the dialog never faded in)")
    res, st = run("site=eightfold&stall=1", hidden=True)
    check("the probe page really was hidden when the dialog opened", st.get("vis") == "hidden", f"vis={st.get('vis')}")
    check("upload succeeds: the stalled fade is run to its end and I Agree clicked",
          res.ok and st["uploads"] == {"resume": 1} and st["chips"] == [name] and st["clicks"] == ["agree"],
          f"ok={res.ok} uploads={st['uploads']} clicks={st['clicks']} {res.detail[:60]}")
    check("the hidden spinner dialog is left hidden and spinning",
          st["busy"] == {"shown": "hidden", "spin": ["running"]}, f"busy={st['busy']}")

    res, st = run("site=eightfold&locked=1")
    check("disabled I Agree -> UPLOAD_FAILED, nothing uploaded",
          not res.ok and st["uploads"] == {} and st["clicks"] == [], f"ok={res.ok} clicks={st['clicks']}")
    check("the failure names the dialog and its buttons",
          '"Data Privacy Agreement" dialog' in res.detail and "Cancel | I Agree" in res.detail, res.detail[-120:])

    print("\nHeron / Workday start page: no input, cookie banner")
    res, st = run("site=heron")
    msg = res.as_agent_message()
    check("no input -> UPLOAD_FAILED counted as no-file-input",
          not res.ok and NO_FILE_INPUT in res.detail and not res.candidates, res.detail[:80])
    check("the agent is told to check its click took effect", "cookie banner" in msg)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


def main() -> int:
    global CDP, RESUME
    results.clear()
    Stability.setUpClass()
    try:
        CDP = Stability.base
        RESUME = Path(Stability.tmp.name) / "resume.pdf"
        RESUME.write_bytes(Stability.pdf.read_bytes())
        return _checks()
    finally:
        Stability.tearDownClass()


if __name__ == "__main__":
    sys.exit(main())
