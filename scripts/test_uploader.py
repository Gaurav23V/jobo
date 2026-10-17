#!/usr/bin/env python3
"""Test the deterministic uploader against every real-world input shape.

module4/uploader.py exists because prompt-level guidance did not make the
agent perform uploads correctly (job393 failed twice on the same shadow-DOM
input). This validates the code path that replaced it, against
scripts/cdp_upload_probe.html which reproduces the five shapes seen on real
ATS portals:

  1. visible <input type=file>              (plain)
  2. hidden input behind a styled button    (Greenhouse, Ashby)
  3. attribute-only input, no id            (Rippling data-testid)
  4. input inside a shadow root             (SmartRecruiters spl-dropzone)
  5. input inside a same-origin iframe      (embedded ATS widgets)

Every check verifies SERVER-SIDE truth: the file name is read back out of the
page's own element.files, so "pass" means the file is genuinely attached, not
that a CDP call returned without error.

Also tests field disambiguation (resume vs cover letter) on a two-slot form,
since putting the resume in the cover-letter box is a silent, real failure.

Prereq: a Chromium with CDP on port 9333 (module4.browser_session starts one).
Run: uv run python scripts/test_uploader.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import browser_session, uploader  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "cdp_upload_probe.html"
TWO_SLOT = REPO / "scripts" / "cdp_upload_probe_two_slot.html"
RESUME = REPO / "personal.example" / "resume.pdf"
CDP = "http://127.0.0.1:9333"

TWO_SLOT_HTML = """<!doctype html>
<html><head><title>Two-slot upload probe</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:40px auto">
<h1>Application</h1>
<label>Cover letter (optional)
  <input type="file" id="cover_letter" name="cover_letter" accept=".pdf,.doc">
</label>
<br><br>
<label>Resume / CV (required)
  <input type="file" id="resume" name="resume" accept=".pdf,.doc">
</label>
<pre id="log"></pre>
<script>
window.__slots = () => JSON.stringify({
  cover: Array.from((document.getElementById('cover_letter').files)||[]).map(f=>f.name),
  resume: Array.from((document.getElementById('resume').files)||[]).map(f=>f.name)
});
</script>
</body></html>
"""


def new_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def close_tab(tab_id: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def eval_in(ws_url: str, expr: str):
    with uploader._Cdp(ws_url) as c:
        r = c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")


def close_all_but_one() -> None:
    """Create a known-live guard BEFORE closing any previous targets.

    /json/close is asynchronous: /json/list may still return the previous
    probe first. Keeping tabs[0] can keep that closing probe while deleting
    the actual guard, leaving zero pages when the pending close completes.
    A freshly created blank tab has no pending close and owns no upload slots.
    """
    guard = new_tab("about:blank")
    for tab in uploader.list_page_targets(CDP):
        if tab["id"] != guard["id"]:
            close_tab(tab["id"])


def main() -> int:
    if not RESUME.is_file():
        print(f"FAIL: no resume fixture at {RESUME}")
        return 1
    if not PROBE.is_file():
        print(f"FAIL: no probe page at {PROBE}")
        return 1
    TWO_SLOT.write_text(TWO_SLOT_HTML)

    try:
        uploader.list_page_targets(CDP)
    except Exception as e:
        print(f"FAIL: no CDP on {CDP}: {e}")
        return 1

    results: list[tuple[str, bool, str]] = []

    # --- The five input shapes, one tab at a time so target selection is
    # unambiguous and each result is attributable. ---
    shapes = [
        ("plain visible input", "plain"),
        ("hidden behind button", "hidden"),
        ("attribute-only (data-testid)", "testid"),
        ("shadow DOM (spl-dropzone shape)", "shadow"),
        ("same-origin iframe", "iframe"),
    ]
    for label, key in shapes:
        close_all_but_one()
        tab = new_tab(f"file://{PROBE}")
        time.sleep(1.5)
        try:
            # Remove the other inputs so only the shape under test can win.
            # Uses the page's own DOM, so the uploader still has to find it.
            strip = f"""
            (() => {{
              const keep = {json.dumps(key)};
              const host = document.getElementById('shadow-host');
              if (keep !== 'plain') document.getElementById('plain-input')?.remove();
              if (keep !== 'hidden') document.getElementById('hidden-input')?.remove();
              if (keep !== 'testid') document.querySelector('[data-testid=input-resume]')?.remove();
              if (keep !== 'shadow' && host) host.remove();
              if (keep !== 'iframe') document.getElementById('frame')?.remove();
              return 'stripped';
            }})()
            """
            eval_in(tab["webSocketDebuggerUrl"], strip)
            time.sleep(0.3)

            res = uploader.upload_file(CDP, str(RESUME), want="resume")
            state_raw = eval_in(tab["webSocketDebuggerUrl"], "window.__uploadState()")
            state = json.loads(state_raw) if state_raw else {}
            landed = state.get(key) or []
            ok = bool(landed) and RESUME.name in landed
            note = res.detail if not ok else f"files={landed}"
            results.append((label, ok, note))
            print(f"  {'PASS' if ok else 'FAIL'}  {label:34s} {note[:90]}")
        finally:
            close_tab(tab["id"])

    # --- Field disambiguation: resume must not land in the cover-letter slot ---
    print()
    close_all_but_one()
    tab = new_tab(f"file://{TWO_SLOT}")
    time.sleep(1.5)
    try:
        res = uploader.upload_file(CDP, str(RESUME), want="resume")
        slots = json.loads(eval_in(tab["webSocketDebuggerUrl"], "window.__slots()") or "{}")
        ok = bool(slots.get("resume")) and not slots.get("cover")
        note = f"resume={slots.get('resume')} cover={slots.get('cover')}"
        results.append(("resume goes to resume slot", ok, note))
        print(f"  {'PASS' if ok else 'FAIL'}  {'resume goes to resume slot':34s} {note}")

        res2 = uploader.upload_file(CDP, str(RESUME), want="cover")
        slots2 = json.loads(eval_in(tab["webSocketDebuggerUrl"], "window.__slots()") or "{}")
        ok2 = bool(slots2.get("cover"))
        note2 = f"resume={slots2.get('resume')} cover={slots2.get('cover')}"
        results.append(("cover request goes to cover slot", ok2, note2))
        print(f"  {'PASS' if ok2 else 'FAIL'}  {'cover request goes to cover slot':34s} {note2}")
    finally:
        close_tab(tab["id"])

    # --- Popup/second-tab discovery: the form is NOT in the active tab ---
    print()
    close_all_but_one()
    base = new_tab("about:blank")
    time.sleep(0.5)
    form_tab = new_tab(f"file://{TWO_SLOT}")
    time.sleep(1.5)
    try:
        res = uploader.upload_file(CDP, str(RESUME), want="resume")
        slots = json.loads(eval_in(form_tab["webSocketDebuggerUrl"], "window.__slots()") or "{}")
        ok = bool(slots.get("resume"))
        results.append(("finds form in a non-active tab", ok, res.detail[:70]))
        print(f"  {'PASS' if ok else 'FAIL'}  {'finds form in a non-active tab':34s} {res.detail[:70]}")
    finally:
        close_tab(form_tab["id"])
        close_tab(base["id"])

    # --- No file input anywhere: must fail cleanly, not crash or lie ---
    # Close EVERY tab first: the persistent browser's leftover tabs (LinkedIn,
    # a previous ATS page) have their own file inputs and would be legitimately
    # found, which is correct behaviour but makes this check meaningless.
    print()
    blank = new_tab("about:blank")
    time.sleep(0.5)
    for t in uploader.list_page_targets(CDP):
        if t["id"] != blank["id"]:
            close_tab(t["id"])
    time.sleep(1.0)
    try:
        remaining = uploader.find_file_inputs(CDP)
        if remaining:
            # Still not a clean environment; report honestly rather than
            # pretending the check ran.
            results.append(
                (
                    "clean failure with no inputs",
                    False,
                    f"could not isolate a blank browser ({len(remaining)} input(s) still visible)",
                )
            )
            print(
                f"  SKIP  {'clean failure with no inputs':34s} "
                f"browser not isolatable ({len(remaining)} inputs)"
            )
        else:
            res = uploader.upload_file(CDP, str(RESUME), want="resume")
            ok = (not res.ok) and "no <input type=file>" in res.detail
            results.append(("clean failure with no inputs", ok, res.detail[:70]))
            print(
                f"  {'PASS' if ok else 'FAIL'}  {'clean failure with no inputs':34s} "
                f"{res.detail[:70]}"
            )
    finally:
        pass

    # --- Directive parsing ---
    print()
    parse_cases = [
        ("NEED_UPLOAD: resume", ("resume", "")),
        ("NEED_UPLOAD: cover", ("cover", "")),
        ("NEED_UPLOAD: cover_letter", ("cover", "")),
        ("NEED_UPLOAD: cv", ("resume", "")),
        ("NEED_UPLOAD:", ("resume", "")),
        ("NEED_UPLOAD: resume | input#awsm-application-file", ("resume", "input#awsm-application-file")),
        ("something else", None),
    ]
    for line, want in parse_cases:
        got = uploader.parse_request(line)
        ok = got == want
        results.append((f"parse {line[:34]!r}", ok, f"{got}"))
        print(f"  {'PASS' if ok else 'FAIL'}  parse {line[:30]:32s} -> {got}")

    # --- accept-attribute filtering: the job393 bug ---
    # A PDF resume was attached to an "Upload profile image" input because
    # scoring ignored `accept`. An image-only input must never be chosen.
    print()
    from module4.uploader import UploadTarget  # local import for clarity

    accept_cases = [
        ("image-only rejects pdf", {"accept": ".png,.jpg,.jpeg", "type": "file"}, False),
        ("image/* rejects pdf", {"accept": "image/*", "type": "file"}, False),
        ("pdf accepted", {"accept": ".pdf,.doc", "type": "file"}, True),
        ("no accept attr allows anything", {"type": "file"}, True),
        ("wildcard allows pdf", {"accept": "*", "type": "file"}, True),
        ("smartrecruiters resume accept", {"accept": ".doc, .dot, .pdf, .resume", "type": "file"}, True),
    ]
    for label, attrs, want_ok in accept_cases:
        t = UploadTarget(backend_node_id=1, attrs=attrs, target_id="t", ws_url="ws")
        got = t.can_accept("resume.pdf")
        ok = got == want_ok
        results.append((f"accept: {label}", ok, f"can_accept={got}"))
        print(f"  {'PASS' if ok else 'FAIL'}  {('accept: ' + label):40s} can_accept={got}")

    # An image slot must score far below a real resume slot.
    img = UploadTarget(
        backend_node_id=1,
        attrs={"accept": ".png,.jpg", "aria-label": "Upload profile image", "type": "file"},
        target_id="t",
        ws_url="ws",
    )
    doc = UploadTarget(
        backend_node_id=2,
        attrs={"accept": ".pdf,.doc", "id": "resume", "type": "file"},
        target_id="t",
        ws_url="ws",
    )
    ok = doc.score_for("resume") > img.score_for("resume")
    results.append(("profile-image input scores below resume input", ok, ""))
    print(
        f"  {'PASS' if ok else 'FAIL'}  {'image input scores below resume input':40s} "
        f"img={img.score_for('resume')} doc={doc.score_for('resume')}"
    )

    TWO_SLOT.unlink(missing_ok=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nFailures:")
        for label, ok, note in results:
            if not ok:
                print(f"  {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
