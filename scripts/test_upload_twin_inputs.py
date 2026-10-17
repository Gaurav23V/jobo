#!/usr/bin/env python3
"""Twin file inputs: pick the right one, and accept widgets that empty their input.

Regression for Fieldfare 1375 (SmartRecruiters oneclick) and Cronus 1380/1396
(Keka), all stuck on 2026-10-03 with 4 NEED_UPLOAD rounds of
"ambiguous upload slots; supply a distinguishing hint".

Measured live on 2026-10-04 (scratch Chromium, real pages, no submit):
  * SmartRecruiters renders two <spl-dropzone> web components whose open shadow
    roots each hold a byte-identical <input type=file id="file-input">. The only
    difference is OUTSIDE the shadow root: the host's data-test
    (apply-with-resume-container vs resume-upload) and the section title
    ("Easy Apply" vs "Resume"). The uploader read neither, so every request,
    hint or not, was refused as ambiguous.
  * After a correct write the widget empties its input within ~15 ms and shows a
    "resume.pdf" chip in its shadow root. The uploader read the empty input as
    "file lost", wrote again (a second upload, which SR answered with HTTP 403)
    and never saw the chip (light-DOM text only; the live page has ~1,800
    shadow roots).
  * Keka (visible input#resume-upload + hidden twin) was already fixed by 7806cbc;
    kept here so the twin tie-break cannot regress it.

Page: scripts/upload_twin_inputs_probe.html. Self-isolated: own headless
Chromium on a random port (never 9333).
Run: bash scripts/run_regression_tests.sh test_upload_twin_inputs
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import uploader  # noqa: E402
from scripts.test_upload_stability import Stability  # noqa: E402

PROBE = Path(__file__).resolve().parent / "upload_twin_inputs_probe.html"
CDP = ""
RESUME: Path
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:66s} {note}")


def open_probe(query: str = "") -> dict:
    url = PROBE.as_uri() + (("?" + query) if query else "")
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
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


def close_tab(tab: dict) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab['id']}", timeout=5).read()
    except Exception:
        pass


def state(tab: dict):
    with uploader._Cdp(tab["webSocketDebuggerUrl"]) as c:
        r = c.call("Runtime.evaluate", {"expression": "window.__state && window.__state()", "returnByValue": True})
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else None


def run(query: str = "", hint: str = ""):
    tab = open_probe(query)
    try:
        res = uploader.upload_file(CDP, str(RESUME), want="resume", hint=hint)
        time.sleep(0.3)
        st = state(tab)
        assert st is not None, "probe state unreadable"
        return res, st
    finally:
        close_tab(tab)


def _checks() -> int:
    name = RESUME.name
    print("SmartRecruiters: two identical shadow inputs, no hint")
    res, st = run()
    check("default request lands in the Resume section, not Easy Apply",
          res.ok and st["uploads"] == {"easy": 0, "resume": 1, "keka_visible": 0, "keka_hidden": 0},
          f"ok={res.ok} uploads={st['uploads']}")
    check("the widget's own chip shows the file (input was emptied)",
          st["chips"]["resume"] == [name], f"chips={st['chips']}")
    check("success cites the site's evidence, not the empty input",
          res.ok and "shows" in res.detail, res.detail[:110])
    check("candidates name host and section so the agent can choose",
          any('data-test=resume-upload' in c and 'under "Resume"' in c for c in res.candidates)
          and any('under "Easy Apply"' in c for c in res.candidates), str(res.candidates)[:110])

    print("\nSmartRecruiters: the agent asks for a specific zone")
    res, st = run(hint='under "Easy Apply"')
    check('hint `under "Easy Apply"` picks the autofill zone',
          res.ok and st["uploads"]["easy"] == 1 and st["uploads"]["resume"] == 0,
          f"ok={res.ok} uploads={st['uploads']}")
    res, st = run(hint="spl-dropzone[data-test=resume-upload]")
    check("hint `spl-dropzone[data-test=resume-upload]` picks the Resume zone",
          res.ok and st["uploads"]["resume"] == 1 and st["uploads"]["easy"] == 0,
          f"ok={res.ok} uploads={st['uploads']}")

    print("\nSmartRecruiters: the widget empties its input but never shows the file")
    res, st = run(query="nochip=1")
    check("no chip -> UPLOAD_FAILED, never a false success",
          not res.ok, f"ok={res.ok} {res.detail[:90]}")
    check("stays within the 2-write cap and never spills into the Easy Apply zone",
          st["uploads"]["resume"] <= 2 and st["uploads"]["easy"] == 0,
          f"uploads={st['uploads']}")

    print("\nKeka: visible input#resume-upload + hidden twin (7806cbc)")
    res, st = run(query="keka=1")
    check("visible Keka input wins, hidden twin untouched",
          res.ok and st["uploads"]["keka_visible"] == 1 and st["uploads"]["keka_hidden"] == 0,
          f"ok={res.ok} uploads={st['uploads']}")

    print("\nUnit: tie-break never invents a choice")
    bare = [uploader.UploadTarget(7, {"type": "file"}, "t", "ws"),
            uploader.UploadTarget(8, {"type": "file"}, "t", "ws")]
    try:
        uploader._select(bare, "resume.pdf", "resume", "")
        check("two indistinguishable inputs still refuse as ambiguous", False)
    except RuntimeError:
        check("two indistinguishable inputs still refuse as ambiguous", True)
    same = [uploader.UploadTarget(9, {"type": "file", "id": "file-input"}, "t", "ws", in_shadow=True,
                                  where_chain="spl-dropzone[data-test=a]", where_heading="Upload"),
            uploader.UploadTarget(10, {"type": "file", "id": "file-input"}, "t", "ws", in_shadow=True,
                                  where_chain="spl-dropzone[data-test=b]", where_heading="Upload")]
    try:
        uploader._select(same, "resume.pdf", "resume", "")
        check("same-named sections with no resume/autofill words still refuse", False)
    except RuntimeError:
        check("same-named sections with no resume/autofill words still refuse", True)

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
