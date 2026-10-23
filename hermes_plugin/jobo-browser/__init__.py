"""Hermes plugin for the Jobo apply agent (enabled only in the `jobo` profile).

  * snapshot_filter.py - page snapshots cut to form controls and status text
  * tab_keeper.py      - one tab once the agent leaves the job listing
  * account_guard.py   - vetoes cookie/storage wipes of the shared profile,
                         LinkedIn/Google sign-in flows, and invented passwords
  * captcha_guard.py   - DataDome bot walls (stop fast), job-board click
                         trackers (no loops), jigsaw sliders (-> NEED_SLIDER)
  * resume_guard.py    - no LinkedIn Easy Apply Submit unless this job's own
                         resume card is the selected one
  * workday_guard.py   - no Workday Save and Continue while a required field
                         is unanswered; names what the site still flags (GHO-72)
  * after_action.py    - click/scroll/press results carry the page after the
                         action, so the agent does not spend a model call on
                         a bare browser_snapshot (GHO-15)

Installed by scripts/setup_hermes_profile.sh as a symlink, so it lives in the
jobo repo and survives `hermes update` (the 2026-09-24 update silently wiped a
local patch to Hermes's own browser code; a plugin is outside that tree).
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot_filter = _load("jobo_browser_snapshot_filter", "snapshot_filter.py")
tab_keeper = _load("jobo_browser_tab_keeper", "tab_keeper.py")
account_guard = _load("jobo_browser_account_guard", "account_guard.py")
captcha_guard = _load("jobo_browser_captcha_guard", "captcha_guard.py")
click_scroller = _load("jobo_browser_click_scroller", "click_scroller.py")
resume_guard = _load("jobo_browser_resume_guard", "resume_guard.py")
workday_guard = _load("jobo_browser_workday_guard", "workday_guard.py")
after_action = _load("jobo_browser_after_action", "after_action.py")


def _ref_name(ref: str) -> str:
    """Accessible name of a snapshot ref ("button Submit application")."""
    return account_guard._REF_NAMES.get(str(ref or "").lstrip("@"), "")


def _notices_kept(text: str | None, notices: list[str]) -> str | None:
    """Make sure every notice is at the top of the snapshot.

    snapshot_filter keeps only '- role' lines, so a notice prepended before
    filtering vanished on any snapshot long enough to be filtered (>1.5k chars).
    """
    if text is None or not notices:
        return text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(data, dict):
        return text
    snap = str(data.get("snapshot") or "")
    missing = [n for n in notices if n not in snap]
    if not missing:
        return text
    data["snapshot"] = "\n".join(missing) + "\n" + snap
    return json.dumps(data, ensure_ascii=False)


def _transform_tool_result(tool_name=None, args=None, result=None, task_id=None, api_request_id=None,
                           **_kwargs):
    # A click/scroll/press result that now carries the page is a navigate-shaped
    # result: the guards and the filter below treat it exactly like one.
    try:
        with_page = after_action.attach(tool_name, args, result, task_id or "", api_request_id or "")
    except Exception as e:  # fail open
        logger.debug("jobo-browser after-action snapshot failed: %s", e)
        with_page = None
    if with_page is not None:
        tool_name, result = "browser_navigate", with_page
    # The guards read the UNFILTERED result (they need the guest-page / wall
    # text the filter would drop); their notices are re-added after filtering.
    annotated = None
    notices: list[str] = []
    try:
        annotated = account_guard.annotate(tool_name, result)
    except Exception as e:  # fail open
        logger.debug("jobo-browser account guard observe failed: %s", e)
    current = annotated if annotated is not None else result
    try:
        seen = captcha_guard.observe(tool_name, result)
        if seen:
            kind, notice = seen
            notices.append(notice)
            annotated = current = captcha_guard.prepend(current, notice, kind) or current
            logger.info("jobo-browser captcha guard: %s wall seen", kind)
    except Exception as e:  # fail open
        logger.debug("jobo-browser captcha guard observe failed: %s", e)
    try:
        filtered = snapshot_filter.transform_result(tool_name, args, current)
    except Exception as e:  # fail open: an unfiltered snapshot beats a broken tool
        logger.debug("jobo-browser snapshot filter failed: %s", e)
        filtered = None
    out = filtered if filtered is not None else annotated
    if out is None and with_page is not None:
        out = with_page
    return _notices_kept(out, notices)


def _pre_tool_call(tool_name=None, args=None, task_id=None, api_request_id=None, **_kwargs):
    try:
        message = after_action.stale_ref_block(tool_name or "", args, task_id or "", api_request_id or "")
        if message:
            return {"action": "block", "message": message}
        message = account_guard.check_tool_call(tool_name or "", args)
        if message:
            logger.warning("jobo-browser account guard blocked %s: %s", tool_name, message[:100])
            return {"action": "block", "message": message}
        message = captcha_guard.check_tool_call(tool_name or "", args)
        if message:
            logger.warning("jobo-browser captcha guard blocked %s: %s", tool_name, message[:100])
            return {"action": "block", "message": message}
        message = resume_guard.check_tool_call(tool_name or "", args, _ref_name)
        if message:
            logger.warning("jobo-browser resume guard blocked %s: %s", tool_name, message[:100])
            return {"action": "block", "message": message}
        message = workday_guard.check_tool_call(tool_name or "", args, _ref_name)
        if message:
            return {"action": "block", "message": message}
        rewrite = account_guard.password_rewrite(tool_name or "", args)
        if rewrite:
            logger.info("jobo-browser account guard: password field gets the account's known password")
            return {"action": "modify", "args": rewrite}
    except Exception as e:  # fail open: a guard bug must not break every tool
        logger.warning("jobo-browser guard skipped: %s", e)
    # Only for a click that is going ahead (no block above). Fails open.
    try:
        click_scroller.before_click(tool_name or "", args, task_id or "")
    except Exception as e:
        logger.debug("jobo-browser click scroller skipped: %s", e)
    return None


def _post_tool_call(tool_name=None, task_id=None, **_kwargs):
    try:
        tab_keeper.after_tool(tool_name or "", task_id or "")
    except Exception as e:  # fail open: a leftover tab beats a broken tool call
        logger.warning("jobo-browser tab keeper skipped: %s", e)
    return None


def register(ctx):
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("post_tool_call", _post_tool_call)
