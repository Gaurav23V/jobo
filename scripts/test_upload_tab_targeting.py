#!/usr/bin/env python3
"""Upload must survive a form that is still mounting, and target the right tab.

TWO distinct causes produce the SAME symptom - "call succeeded but page reports
files=[(none)]" - and neither is the detached-node case already covered by
test_upload_stale_node.py. Both were found by running real jobs.

1. THE FORM WAS STILL RENDERING (the real Westbay/Rippling cause, measured
   from the live log on 2026-09-13):

       12:26:09  3 inputs found -> all report files=[(none)]
       12:26:13  2 inputs found -> same
       12:26:33  6 inputs found -> SUCCESS on the first candidate

   Nothing was detached, no tab was wrong. React had not finished mounting, so
   the file went onto a placeholder input that was replaced moments later.
   `upload_file` now observes the selected logical slot within a bounded
   settle budget and retries at most once without switching tabs.

   Worth recording: my first theory was that duplicate tabs made the ranking
   pick a background copy. Measuring it killed that theory - both the old and
   new ranking chose the SAME winner on the live page, so the tie-break was
   not what fixed anything. The growing candidate count is what gave the real
   answer. Do not re-derive the tab theory from the symptom alone.

2. Tab position as a TIEBREAK is still worth having (two copies of a form do
   occur, and their inputs are byte-identical), but it must never outrank a
   better match: Rippling opens the real form in a popup, so the best input
   legitimately lives on a background target.

Prereq: Chromium installed; starts its own temporary profile on a random port.
Run: uv run python scripts/test_upload_tab_targeting.py
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

REPO = Path(__file__).resolve().parent.parent
LATE_PROBE = REPO / "scripts" / "upload_late_mount_probe.html"
RESUME: Path
CDP = ""  # Populated only by the isolated harness in main().

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} {note}")


def new_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def close_tab(tab_id: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def _browser_alive() -> bool:
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
        return True
    except Exception:
        return False


def state(ws: str) -> dict:
    with uploader._Cdp(ws) as c:
        r = c.call(
            "Runtime.evaluate",
            {"expression": "window.__formState()", "returnByValue": True},
        )
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw else {}


def _checks() -> int:
    for p in (LATE_PROBE, RESUME):
        if not p.is_file():
            print(f"FAIL: missing {p}")
            return 1

    print("THE BUG: the real input mounts AFTER the first upload attempt")
    tab = new_tab(f"file://{LATE_PROBE}")
    time.sleep(0.25)  # deliberately early - the real input is not there yet
    ws = tab["webSocketDebuggerUrl"]
    try:
        before = state(ws)
        check(
            "probe starts with only a placeholder input",
            before.get("mounted") is False,
            f"mounted={before.get('mounted')}",
        )
        r = uploader.upload_file(CDP, str(RESUME), want="resume")

        after = state(ws)
        check(
            "early upload returns success only on the settled REAL input",
            r.ok and after.get("real") == ["resume.pdf"],
            f"ok={r.ok} real={after.get('real')}",
        )
        check(
            "browser is still alive afterwards",
            _browser_alive(),
            "",
        )
    finally:
        close_tab(tab["id"])

    print("\nRanking: a better MATCH still beats tab position")
    # Rippling's real form opens in a popup, so a background target must be
    # able to win. Tab is a tiebreak, never the primary key.
    active_unrelated = uploader.UploadTarget(
        backend_node_id=1, attrs={"id": "unrelated"}, target_id="a", ws_url="", tab_rank=0
    )
    popup_resume = uploader.UploadTarget(
        backend_node_id=2,
        attrs={"data-testid": "input-resume", "accept": ".pdf"},
        target_id="b",
        ws_url="",
        tab_rank=3,
    )
    check(
        "a popup's resume input outscores an unrelated active-tab input",
        popup_resume.score_for("resume") > active_unrelated.score_for("resume"),
        f"popup={popup_resume.score_for('resume')} active={active_unrelated.score_for('resume')}",
    )

    # And on a genuine tie, the active tab wins.
    def rank(t):
        return (-t.score_for("resume"), t.tab_rank, t.depth)

    twin_active = uploader.UploadTarget(
        backend_node_id=3,
        attrs={"data-testid": "input-resume", "accept": ".pdf"},
        target_id="a",
        ws_url="",
        tab_rank=0,
    )
    twin_stale = uploader.UploadTarget(
        backend_node_id=4,
        attrs={"data-testid": "input-resume", "accept": ".pdf"},
        target_id="b",
        ws_url="",
        tab_rank=2,
    )
    winner = sorted([twin_stale, twin_active], key=rank)[0]
    check(
        "identical inputs: the ACTIVE tab wins the tie",
        winner.backend_node_id == twin_active.backend_node_id,
        f"winner tab_rank={winner.tab_rank}",
    )

    print("\nThe runner clears leftover tabs BEFORE a job, not only after")
    src = (REPO / "module4" / "runner.py").read_text()
    healthy_at = src.index("healthy_cdp = browser_session.ensure_healthy")
    applying_at = src.index("Applying (attempt")
    check(
        "close_extra_tabs runs between the health check and the attempt",
        src.find("browser_session.close_extra_tabs()", healthy_at, applying_at) != -1,
        "",
    )

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nFailures:")
        for label, ok, note in results:
            if not ok:
                print(f"  {label}: {note}")
        return 1
    return 0


def main() -> int:
    global CDP, RESUME
    results.clear()
    Stability.setUpClass()
    try:
        CDP = Stability.base
        RESUME = Path(Stability.tmp.name) / 'resume.pdf'
        RESUME.write_bytes(Stability.pdf.read_bytes())
        return _checks()
    finally:
        Stability.tearDownClass()


if __name__ == "__main__":
    raise SystemExit(main())
