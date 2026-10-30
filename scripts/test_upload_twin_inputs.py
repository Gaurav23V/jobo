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

Dayforce (Interactive Brokers 2127, 2026-10-25): two `input#jobPostingApplication_
files_resume` with no <form>, test id or custom element to tell them apart; only
aria-required="true" on the "Resume Upload" one. The slot was chosen, then every
request failed "ambiguous original upload identity" (4 rounds). Page:
scripts/upload_dayforce_twins_probe.html.

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
DAYFORCE = Path(__file__).resolve().parent / "upload_dayforce_twins_probe.html"
CDP = ""
RESUME: Path
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:66s} {note}")


def open_probe(query: str = "", probe: Path = PROBE) -> dict:
    url = probe.as_uri() + (("?" + query) if query else "")
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


def run(query: str = "", hint: str = "", probe: Path = PROBE):
    tab = open_probe(query, probe)
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

    print("\nDayforce: two input#jobPostingApplication_files_resume, one aria-required")
    for hint in ("", 'under "Resume Upload"', 'aria-required="true"'):
        res, st = run(hint=hint, probe=DAYFORCE)
        check(f"hint {hint or '(none)'!r} writes the Resume Upload slot once",
              res.ok and st["uploads"] == {"import": 0, "upload": 1, "cover": 0, "extra": 0},
              f"ok={res.ok} uploads={st['uploads']} {res.detail[:60]}")
    res, st = run(hint='under "Candidate Info"', probe=DAYFORCE)
    check('hint `under "Candidate Info"` picks the import twin (identity: no aria-required)',
          res.ok and st["uploads"] == {"import": 1, "upload": 0, "cover": 0, "extra": 0},
          f"ok={res.ok} uploads={st['uploads']} {res.detail[:60]}")
    res, st = run(query="bare=1", probe=DAYFORCE)
    check("attribute-identical twins still refuse, nothing written",
          not res.ok and "ambiguous" in res.detail and sum(st["uploads"].values()) == 0,
          f"ok={res.ok} uploads={st['uploads']} {res.detail[:60]}")

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
    styled = [uploader.UploadTarget(11, {"type": "file", "id": "r", "class": "a"}, "t", "ws",
                                    where_heading="Resume"),
              uploader.UploadTarget(12, {"type": "file", "id": "r", "class": "b"}, "t", "ws",
                                    where_heading="Cover")]
    try:
        uploader._select(styled, "resume.pdf", "resume", "")
        check("twins that differ only in class (unstable) still refuse", False)
    except RuntimeError as e:
        check("twins that differ only in class (unstable) still refuse", "identity" in str(e), str(e))

    print("\nRetry notice after an ambiguous-upload refusal (2127's stored status)")
    from module4 import prompts
    stored = ('Dayforce form has two identical "jobPostingApplication_files_resume" file inputs '
              '(one under "Candidate Info" import, one under "Resume Upload") that the upload '
              "pipeline could not disambiguate even with distinguishing hints (aria-required, "
              "section quote), so no resume could be verified as attached and the upload "
              "request cap (4) was reached before Submit")

    def retry(previous: str | None) -> str:
        return prompts.user_prompt(
            job_id=2127, company="Acme", title="Dev", url="https://jobs.dayforcehcm.com/x",
            resume_path="/tmp/r.pdf", cover_path=None, previous_failure=previous)

    note = retry(stored)
    check("asks NEED_UPLOAD again instead of quoting the refusal",
          "end your turn with NEED_UPLOAD: resume" in note and "disambiguate even" not in note)
    check("never sends the agent to DOM.setFileInputFiles", "setFileInputFiles" not in note)
    check("uploader's own refusal text gets the same notice",
          "NEED_UPLOAD: resume (rule 5)" in retry("4 file input(s) found; ambiguous original upload identity"))
    check("cap-only status gets it too",
          "NEED_UPLOAD: resume (rule 5)" in retry("Agent exceeded 4 upload requests; refusing further ones."))
    other = retry("resume upload failed: no file input")
    check("other upload failures stay quoted, generic notice no longer says setFileInputFiles",
          "no file input" in other and "setFileInputFiles" not in other)
    check("captcha failure keeps the generic notice", "(rule 5)" not in retry("captcha wall on apply page"))
    check("first attempt has no notice", "RETRY NOTICE" not in retry(None))

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
