"""Return the page as it is AFTER a browser_click / browser_scroll / browser_press.

Why (GHO-15, measured on the 2026-10-11 and 2026-10-14 runs, 96 apply jobs,
3,093 model calls): Hermes answers a click with `{"success": true, "clicked":
"@e21"}` and nothing about the page, so the agent has to spend its NEXT model
call on a bare browser_snapshot to see what the click did. 521 of 665 click
turns (78%) were followed by exactly that, plus 61 after scroll/press: 19% of
every model call the apply agent made, each one re-sending the job's whole
~40k-token context, at ~5 s per call. Hermes already does this for
browser_navigate (its own _attach_auto_snapshot); this extends it to the
other page-changing actions.

Refs: a new snapshot renumbers agent-browser's refs. Browser actions in one
model response run one after another, so a later call in the SAME response
still carries refs from the page as the model last saw it. `stale_ref_block`
refuses those (one cheap, explicit refusal) instead of letting an old @e12
land on whatever is @e12 now.

Runs from transform_tool_result through the same Hermes browser session the
action used, and fails open: on any error the action's own result is returned
unchanged and the agent snapshots as before. JOBO_AUTO_SNAPSHOT=0 turns it off.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

TRIGGER_TOOLS = {"browser_click", "browser_scroll", "browser_press"}
# Clicks that navigate are often still loading when agent-browser returns.
SETTLE_MAX_SEC = 3.0
SETTLE_POLL_SEC = 0.25
NOTE = (
    "[jobo: page as it is after this action. Its refs replace every earlier ref; "
    "you do not need browser_snapshot unless the page is still loading.]"
)
STALE_REF_MESSAGE = (
    "Not run: an earlier action in this same response changed the page and its result "
    "carries a fresh snapshot, so the refs you chose for this call may now point at "
    "different elements. Pick the ref again from that snapshot and repeat this call."
)

# task_id -> api_request_id of the model response whose refs were superseded.
_superseded: dict[str, str] = {}


def enabled() -> bool:
    return os.environ.get("JOBO_AUTO_SNAPSHOT", "1").strip() != "0"


def reset_state() -> None:
    _superseded.clear()


def _bt():
    from tools import browser_tool as bt  # Hermes internals, imported lazily

    return bt


def _ready_state(task_id: str) -> str:
    bt = _bt()
    key = bt._last_session_key(task_id or "default")
    res = bt._session._run_browser_command(key, "eval", ["document.readyState"], timeout=5)
    data = res.get("data") if isinstance(res, dict) else None
    raw = data.get("result") if isinstance(data, dict) else ""
    return str(raw or "").strip().strip('"').strip("'")


def settle(task_id: str, ready_state=None, sleep=time.sleep, clock=time.monotonic) -> None:
    """Wait (bounded) for the document to finish loading."""
    probe = ready_state or _ready_state
    end = clock() + SETTLE_MAX_SEC
    while True:
        try:
            if probe(task_id) == "complete":
                return
        except Exception as e:  # fail open: snapshot whatever is there
            logger.debug("jobo after-action: readyState probe failed: %s", e)
            return
        if clock() >= end:
            return
        sleep(SETTLE_POLL_SEC)


def _snapshot(task_id: str) -> str:
    return _bt().browser_snapshot(task_id=task_id or None)


def attach(tool_name, args, result, task_id="", api_request_id="", snapshot=None, ready_state=None,
           sleep=time.sleep) -> str | None:
    """The action's result with the post-action snapshot merged in, or None to leave it."""
    if tool_name not in TRIGGER_TOOLS or not enabled() or not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("success") is not True or "snapshot" in data:
        return None
    try:
        settle(task_id, ready_state=ready_state, sleep=sleep)
        snap = json.loads((snapshot or _snapshot)(task_id))
    except Exception as e:  # fail open
        logger.debug("jobo after-action: snapshot after %s failed: %s", tool_name, e)
        return None
    if not isinstance(snap, dict) or snap.get("success") is not True or not isinstance(snap.get("snapshot"), str):
        return None
    # The page only. Hermes's supervisor fields (frame_tree, pending dialogs)
    # can still describe the listing tab here: tab_keeper moves the supervisor
    # in post_tool_call, which runs after this hook.
    data["snapshot"] = snap["snapshot"]
    if "element_count" in snap:
        data["element_count"] = snap["element_count"]
    # A top-level key, not a snapshot line: the snapshot filter keeps only
    # "- role" lines and would drop it.
    data["jobo_page_after_action"] = NOTE
    if api_request_id:
        _superseded[task_id or "default"] = api_request_id
    return json.dumps(data, ensure_ascii=False)


def stale_ref_block(tool_name: str, args, task_id="", api_request_id="") -> str | None:
    """Refusal for a ref-carrying call made in a response whose refs were superseded."""
    if not api_request_id or not isinstance(args, dict) or not str(args.get("ref") or "").strip():
        return None
    if _superseded.get(task_id or "default") != api_request_id:
        return None
    logger.info("jobo after-action: refused %s %s, refs superseded in this response", tool_name, args.get("ref"))
    return STALE_REF_MESSAGE
