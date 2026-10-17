#!/usr/bin/env python3
"""Avature forms that ran out of steps just before the end (Wingtip 1318, Southgate 1419).

Live failures, 2026-10-03 (steps counted from the jobo profile's state.db):
  * Southgate 1419, 80/80 steps. 26 went to finding the upload step's Continue:
    the resume input and that Continue sit in a display:none fieldset that only
    the "From Device" toggle opens, and our uploader filled the hidden input, so
    the upload "succeeded" with no Continue on screen.
  * Both jobs: Hermes cuts a snapshot over 15k chars and stores the full tree;
    the jobo snapshot filter then dropped the "[... N more lines truncated]"
    note. 45 of 53 compact snapshots on these forms ended at the Country
    <select>, with nothing saying the page went on. Password, the consent box
    and Continue never appeared; the agents scrolled and probed for them.
  * Both jobs: the required consent box has an EMPTY <label for>; its question
    is the fieldset legend. NEED_FIELDS listed it as "6388 | 6388" /
    "7065 | 7065", and NEED_CLICK "I have read and agree to ..." matched nothing
    (Wingtip 1318: 4 failed NEED_CLICKs, 33 steps on the consent).

Runs against scripts/avature_register_probe.html (markup copied from both live
pages). Self-isolated: own headless Chromium on a random port, never 9333.
Nothing on the probe submits; the test also asserts no Continue was pressed.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module4 import clicker, form_filler as ff, uploader  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("avature_register_probe.html")
PLUGIN = ROOT / "hermes_plugin" / "jobo-browser" / "snapshot_filter.py"

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def _load_filter():
    spec = importlib.util.spec_from_file_location("jobo_snapshot_filter_avature", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hermes_truncate(text: str, stored: str, max_chars: int = 15000) -> str:
    """Same cut and note as Hermes tools/browser_tool_snapshot._truncate_snapshot."""
    lines = text.split("\n")
    out, chars = [], 0
    reserve = min(110 + len(stored), max_chars // 2)
    for line in lines:
        if chars + len(line) + 1 > max_chars - reserve:
            break
        out.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(out)
    out.append(f'\n[... {remaining} more lines truncated \u2014 full snapshot: '
               f'read_file path="{stored}" offset={len(out) + 1} limit=200]')
    return "\n".join(out)


def _register_tree() -> str:
    """Accessibility tree shaped like Southgate's Register page (19-21k chars)."""
    rows = ["- main", "  - heading \"Software Engineer I\" [level=2, ref=e1]",
            "  - textbox \"First Name\" [required, ref=e20]: Test",
            "  - combobox \"Country\" [expanded=false, ref=e24]: Select a country"]
    rows += [f"    - option \"Country number {i:03d} of the long list\" [ref=e{100 + i}]" for i in range(250)]
    rows += ["  - textbox \"City\" [required, ref=e26]",
             "  - textbox \"Password\" [required, ref=e29]",
             "  - group \"I Acknowledge\"",
             "    - link \"here\" [ref=e320]",
             "    - checkbox [checked=false, ref=e321]",
             "  - button \"Continue\" [ref=e322]",
             "  - button \"Cancel\" [ref=e323]"]
    for i in range(40):  # job-description prose the filter drops
        rows.append(f"  - paragraph\n    - StaticText \"Benefits and culture paragraph {i} " + "x" * 120 + "\"")
    return "\n".join(rows)


def test_snapshot_filter(tmp: Path):
    sf = _load_filter()
    full = _register_tree()
    web = tmp / "cache" / "web"
    web.mkdir(parents=True)
    stored = web / "browser-snapshot-0123abcd45.txt"
    stored.write_text(full)
    cut = _hermes_truncate(full, str(stored))
    check("fixture: Hermes' cut drops Password/consent/Continue", len(full) > 15000 and "checkbox" not in cut
          and "more lines truncated" in cut, f"len={len(full)}")

    out = json.loads(sf.transform_result("browser_snapshot", {}, json.dumps({"success": True, "snapshot": cut})))
    snap = out["snapshot"]
    check("truncated snapshot: filtered view comes from the stored FULL tree",
          "checkbox [checked=false" in snap and 'button "Continue"' in snap and 'textbox "Password"' in snap,
          snap[-400:])
    check("truncated snapshot: expanded view is still compact (< 4k chars)", len(snap) < 4000, str(len(snap)))

    gone = _hermes_truncate(full, str(web / "browser-snapshot-ffffffffff.txt"))
    snap2 = json.loads(sf.transform_result("browser_snapshot", {}, json.dumps({"snapshot": gone})))["snapshot"]
    check("stored file missing: view SAYS the page continues and points at NEED_FIELDS",
          "page CONTINUES below" in snap2 and "NEED_FIELDS" in snap2, snap2[-300:])

    def view(path: str) -> str:
        res = sf.transform_result("browser_snapshot", {}, json.dumps({"snapshot": _hermes_truncate(full, path)}))
        return json.loads(res)["snapshot"] if res else ""

    outside = tmp / "browser-snapshot-0123abcd45.txt"  # right name, not under cache/web
    outside.write_text('- button "SECRET" [ref=e9]')
    snap3 = view(str(outside))
    check("a path outside cache/web is never read", "SECRET" not in snap3 and "page CONTINUES" in snap3)
    passwd = view("/etc/passwd")
    check("an arbitrary path named in page text is never read", "root:" not in passwd and "page CONTINUES" in passwd)

    # Untruncated: same as before the fix (filtered when that shrinks it,
    # otherwise left alone).
    plain = "\n".join(rows for rows in _register_tree().split("\n")[:300])
    res = sf.transform_result("browser_snapshot", {}, json.dumps({"snapshot": plain}))
    check("untruncated snapshot: plain filter, unchanged behaviour",
          res is not None and json.loads(res)["snapshot"] == sf.filter_snapshot(plain))
    dense = json.dumps({"snapshot": "- button \"Go\" [ref=e1]\n" * 100})
    check("untruncated snapshot that cannot shrink is left alone",
          sf.transform_result("browser_snapshot", {}, dense) is None)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jobo-avature-budget-", ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        test_snapshot_filter(tmp)
        pdf = tmp / "resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
        profile = tmp / "profile"
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + str(profile),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = profile / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            with sync_playwright() as p:
                page = p.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
                state = lambda: page.evaluate("() => window.__state()")
                page.goto(PROBE.as_uri())

                # 1. Upload into the collapsed "From Device" section.
                check("probe: upload section starts hidden", not state()["sectionShown"])
                res = uploader.upload_file(cdp, str(pdf), want="resume")
                s = state()
                check("upload: file attached and verified", res.ok and s["files"] == ["resume.pdf"], res.detail)
                check("upload: hidden section opened, its Continue now visible", s["sectionShown"] and
                      page.locator("#uploadFileResume").is_visible(), json.dumps(s))
                check("upload: agent is told the step's Continue is now visible",
                      "Continue" in res.detail and "now visible" in res.detail, res.detail)
                check("upload: toggle clicked once, step Continue NOT pressed",
                      s["toggleClicks"] == 1 and s["uploadContinueClicks"] == 0, json.dumps(s))
                again = uploader.upload_file(cdp, str(pdf), want="resume")
                check("upload again with section already open: no extra toggle click",
                      again.ok and state()["toggleClicks"] == 1, json.dumps(state()))

                # 2. Consent box with an empty <label for>: named by its legend.
                ls = ff.list_fields(cdp)
                ff.remember_listing(cdp, ls)
                by_id = {f.meta.split(" | ")[-1]: f for f in ls.fields}
                consent = next((f for f in ls.fields if f.kind == "checkbox" and f.required), None)
                check("list_fields: consent listed as 'I Acknowledge *', not '7065 | 7065'",
                      consent is not None and consent.label.startswith("I Acknowledge"),
                      consent.as_line() if consent else "none")
                check("list_fields: multi-box fieldset keeps each box's own label",
                      by_id.get("6383_2") is not None and by_id["6383_2"].label == "Women at Wingtip",
                      str([f.label for f in ls.fields]))
                shift = [f.label for f in ls.fields if "9000" in f.meta]
                check("list_fields: legend is NOT given to two unlabelled boxes sharing a fieldset",
                      shift and not any("Pick your shifts" in lb for lb in shift), str(shift))

                r = clicker.click(cdp, "I Acknowledge")
                s = state()
                check("NEED_CLICK 'I Acknowledge' ticks the consent box", r.ok and s["consent"], r.note)
                check("consent click pressed neither Continue", s["registerContinueClicks"] == 0
                      and s["uploadContinueClicks"] == 0, json.dumps(s))
                miss = clicker.click(cdp, "Pick your shifts")
                check("legend of a multi-box fieldset still matches nothing (no guess)", not miss.ok, miss.note)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
