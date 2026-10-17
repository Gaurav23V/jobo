"""Scroll a browser_click target into view, instantly, before the click lands.

Redpine 1485 (2026-10-11 retry): the agent's browser_click on "Apply now" did
nothing, twice, and the job was reported stuck ("broken apply flow"). The
button's top edge sat at y=586 in a 577-591 px tall viewport. agent-browser's
`click @ref` dispatches the mouse at the element's box centre without making
sure it is in view, so the press landed below the viewport and the page saw a
click on <html>. Reproduced with agent-browser --cdp on a scratch Chromium
(2/2 dead clicks); `scrollintoview @ref` first opened the modal 2/2.

Second half, measured on the live apply browser (2026-10-12): Redpine's <html>
has `scroll-behavior: smooth`, and in the headful jobo Chromium the animated
scroll only STARTS about 1.1 s after `scrollintoview` returns (button box y
586 -> 584 for a second, then 465). A click sent in between still hit <html>,
and "wait until the position stops changing" cannot see an animation that has
not begun. So the scroll is made instant: inline `scroll-behavior: auto
!important` on <html> and <body> for the scroll, then the page's own inline
values are put back. Measured: box at its final y=465 by 0.15 s.

Runs from pre_tool_call, through the same Hermes browser session the click
will use, and fails open: if anything errors, the click goes ahead as before.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r"^@?(e\d+)$")
SCROLL_TIMEOUT_SEC = 10

_INSTANT_ON_JS = (
    "(() => { const h = document.documentElement, b = document.body; if (!h) return 0;"
    " window.__joboScrollBehavior = [h, b].map(e => e ? [e.style.getPropertyValue('scroll-behavior'),"
    " e.style.getPropertyPriority('scroll-behavior')] : null);"
    " [h, b].forEach(e => e && e.style.setProperty('scroll-behavior', 'auto', 'important'));"
    " return 1; })()"
)
_INSTANT_OFF_JS = (
    "(() => { const saved = window.__joboScrollBehavior; delete window.__joboScrollBehavior;"
    " [document.documentElement, document.body].forEach((e, i) => { if (!e) return;"
    " const s = saved && saved[i]; if (s && s[0]) e.style.setProperty('scroll-behavior', s[0], s[1]);"
    " else e.style.removeProperty('scroll-behavior'); }); return 1; })()"
)


def ref_of(args) -> str | None:
    """'@e19' for a browser_click whose ref looks like a snapshot ref, else None."""
    if not isinstance(args, dict):
        return None
    m = _REF_RE.match(str(args.get("ref") or "").strip())
    return f"@{m.group(1)}" if m else None


def _run(task_id: str, command: str, args: list[str]) -> dict:
    from tools import browser_tool as bt  # Hermes internals, imported lazily

    key = bt._last_session_key(task_id or "default")
    return bt._session._run_browser_command(key, command, args, timeout=SCROLL_TIMEOUT_SEC)


def _ok(res) -> bool:
    return bool(isinstance(res, dict) and res.get("success"))


def before_click(tool_name: str, args, task_id: str, runner=None) -> bool:
    """Instantly scroll the click's target into view. True if the scroll succeeded."""
    if tool_name != "browser_click" or os.environ.get("JOBO_CLICK_SCROLL", "1") == "0":
        return False
    ref = ref_of(args)
    if not ref:
        return False
    run = runner or _run
    instant = False
    try:
        instant = _ok(run(task_id, "eval", [_INSTANT_ON_JS]))
        result = run(task_id, "scrollintoview", [ref])
    except Exception as e:  # fail open: the click still runs
        logger.debug("jobo click scroller: scrollintoview %s raised %s", ref, e)
        result = None
    finally:
        if instant:
            try:
                run(task_id, "eval", [_INSTANT_OFF_JS])
            except Exception as e:
                logger.debug("jobo click scroller: restoring scroll-behavior raised %s", e)
    if not _ok(result):
        logger.warning("jobo click scroller: scrollintoview %s failed: %s", ref, str(result)[:200])
        return False
    logger.info("jobo click scroller: scrolled %s into view before the click (instant=%s)", ref, instant)
    return True
