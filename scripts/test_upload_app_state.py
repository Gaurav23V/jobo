#!/usr/bin/env python3
"""Upload success must mean the SITE accepted the file, not just input.files.

Regression for jobs 721 (Alpineer, Oracle via Phenom), 831 (Avocet, Taleo via
Phenom) and 919 (Woodgrove, Lever), measured live on 2026-10-03:

  1. The old `_attach` dispatched a synthetic input/change pair on top of the
     trusted pair `DOM.setFileInputFiles` already fires. Apps that upload on
     `change` uploaded twice; Alpineer then showed "Unable to upload the resume"
     and Lever "Couldn't auto-read resume.", while our FileList check passed
     and the agent was told UPLOAD_DONE.
  2. Alpineer/Avocet raise alert("You have Uploaded Resume Successfully.") after a
     good upload. Unanswered, it freezes the page's JS, so our own readback
     hung and timed out.
  3. Avocet's resume and attachment inputs have no attributes at all, so the
     two tied and every request failed as "ambiguous", hints included.

Page: scripts/upload_app_state_probe.html (replica of the live Phenom DOM).
Needs a scratch Chromium with CDP; set JOBO_TEST_CDP (default
http://127.0.0.1:9333, which is the pipeline's browser: do not run this
during a live run, point it at your own browser instead).
Run: JOBO_TEST_CDP=http://127.0.0.1:9471 uv run python scripts/test_upload_app_state.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import uploader  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PAGE = (REPO / "scripts" / "upload_app_state_probe.html").as_uri()
RESUME = REPO / "personal.example" / "resume.pdf"
CDP = os.environ.get("JOBO_TEST_CDP", "http://127.0.0.1:9333").rstrip("/")


def _tabs():
    return json.load(urllib.request.urlopen(f"{CDP}/json/list"))


def _open_only(url: str) -> dict:
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    tab = json.load(urllib.request.urlopen(req))
    for t in _tabs():
        if t.get("type") == "page" and t["id"] != tab["id"]:
            urllib.request.urlopen(urllib.request.Request(f"{CDP}/json/close/{t['id']}", method="PUT"))
    time.sleep(1.0)
    return tab


def _state(tab: dict) -> dict:
    with uploader._Cdp(tab["webSocketDebuggerUrl"]) as c:
        c.call("Page.enable")
        c.pump(0.5)  # answers any alert still open so the read can run
        r = c.call("Runtime.evaluate", {"expression": "window.__appState()", "returnByValue": True})
        return json.loads(r["result"]["result"]["value"])


def unit_checks() -> int:
    fails = 0
    resume = uploader.UploadTarget(1, {"type": "file"}, "t", "ws",
                                   context="resume-upload-wrapper drives cloud-options upload resume")
    other = uploader.UploadTarget(2, {"type": "file"}, "t", "ws",
                                  context="file-label upload from device attachments")
    cases = [
        ("context breaks the attribute-less tie", resume.score_for("resume") > other.score_for("resume")),
        ("hint 'input#jobo-resume-input' matches id", uploader._hint_matches(
            uploader.UploadTarget(3, {"type": "file", "id": "jobo-resume-input"}, "t", "ws"), "input#jobo-resume-input")),
        ("hint 'input under div.resume-upload-wrapper' matches context",
         uploader._hint_matches(resume, "input under div.resume-upload-wrapper")),
        ("that hint does not match the other slot", not uploader._hint_matches(other, "input under div.resume-upload-wrapper")),
        ("legacy attr-blob hint still works", uploader._hint_matches(
            uploader.UploadTarget(4, {"type": "file", "name": "resume"}, "t", "ws"), "name=resume")),
    ]
    # Keka (Northfold 974): visible input#resume-upload.resume-upload + hidden twin .resume-upload.
    keka_vis = uploader.UploadTarget(5, {"type": "file", "id": "resume-upload", "class": "resume-upload"}, "t", "ws",
                                     context="resume-upload-container __rendered__")
    keka_hid = uploader.UploadTarget(6, {"type": "file", "class": "resume-upload"}, "t", "ws",
                                     context="resume-upload-link resume-upload-button")
    try:
        picked = uploader._select([keka_hid, keka_vis], "resume.pdf", "resume", "")
        cases.append(("Keka twin inputs: rendered one wins instead of 'ambiguous'", picked.backend_node_id == 5))
    except Exception as e:
        cases.append((f"Keka twin inputs: rendered one wins instead of 'ambiguous' ({e})", False))
    try:
        uploader._select([uploader.UploadTarget(7, {"type": "file"}, "t", "ws"),
                          uploader.UploadTarget(8, {"type": "file"}, "t", "ws")], "resume.pdf", "resume", "")
        cases.append(("two indistinguishable hidden inputs still refuse", False))
    except RuntimeError:
        cases.append(("two indistinguishable hidden inputs still refuse", True))
    for name, ok in cases:
        print(("PASS " if ok else "FAIL ") + name)
        fails += not ok
    return fails


def live_checks() -> int:
    fails = 0

    tab = _open_only(PAGE)
    r = uploader.upload_file(CDP, str(RESUME), want="resume")
    st = _state(tab)
    ok = (r.ok and st["uploads"] == 1 and not st["failed"] and st["resume"] == ["resume.pdf"]
          and st["other"] == [] and "resume.pdf" in st["status"] and "Uploaded Resume Successfully" in r.detail)
    print(("PASS " if ok else "FAIL ") + f"Phenom replica: one upload, app shows file, alert answered | {r.ok} {r.detail[:160]} | {st}")
    fails += not ok

    tab = _open_only(PAGE + "?reject=1")  # /json/new drops a #fragment
    r = uploader.upload_file(CDP, str(RESUME), want="resume")
    st = _state(tab)
    ok = (not r.ok) and "rejected" in r.detail and st["resume"] == ["resume.pdf"]
    print(("PASS " if ok else "FAIL ") + f"site rejects -> UPLOAD_FAILED even though input.files holds the file | {r.ok} {r.detail[:160]}")
    fails += not ok
    return fails


def main() -> int:
    fails = unit_checks() + live_checks()
    print(f"{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
