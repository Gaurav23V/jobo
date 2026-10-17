"""Keep the apply agent on ONE browser tab once it leaves the job listing.

Why: LinkedIn's "Apply on company website" opens the employer's ATS in a new
tab. Hermes then has two views of the browser that can disagree:

  * browser_snapshot / click / type go through agent-browser, which follows
    the click into the new tab;
  * browser_console(expression=...), the pending-dialog watcher and the
    frame metadata on snapshots go through Hermes's CDP "supervisor", which
    stays attached to the FIRST tab - the listing.

So the agent reads location.href from one page while filling another, JS
dialogs on the form go unnoticed, and after a NEED_* handoff (a new hermes
process) agent-browser itself can reattach to the listing tab. Verified live
on 2026-09-28 with scripts/test_pipeline_controls_e2e.py on both the old and
the new jobo code. A local patch to Hermes fixed this on 2026-09-13; the
2026-09-24 `hermes update` removed it and it no longer applies.

The jobo flow never needs the listing tab again once the agent is on the
employer's site, so this closes it: with a single page left, every Hermes
view - and every later resumed process - lands on the right one. Then the
supervisor is restarted so it attaches to that page. Only tabs showing the job's own
listing URL (JOBO_SOURCE_URL, set by module4/agent_driver.py) are ever
closed, and never while agent-browser is still on the listing.

Runs from a post_tool_call hook, so any failure is skipped, never fatal.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

TRIGGER_TOOLS = {"browser_click", "browser_press"}


def _page_key(url: str) -> tuple[str, str, str]:
    p = urlsplit(url or "")
    return (p.scheme, (p.hostname or "").lower(), p.path.rstrip("/"))


def same_page(a: str, b: str) -> bool:
    """Same scheme, host and path; query strings (tracking params) ignored."""
    return bool(a and b) and _page_key(a) == _page_key(b)


def tabs_to_close(page_targets: list[dict], active_url: str, source_url: str) -> list[str]:
    """Target ids of listing tabs that can go, given where agent-browser is now.

    Nothing is closed unless the agent is demonstrably elsewhere, and the
    last remaining page is never closed.
    """
    if not active_url or not source_url or same_page(active_url, source_url):
        return []
    listing = [t["id"] for t in page_targets if same_page(t.get("url", ""), source_url)]
    others = [t for t in page_targets if not same_page(t.get("url", ""), source_url)]
    if not others:
        return []
    return listing


def _cdp_http_base() -> str | None:
    raw = os.environ.get("BROWSER_CDP_URL", "").strip()
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def _page_targets(base: str) -> list[dict]:
    with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
        targets = json.load(resp)
    return [t for t in targets if t.get("type") == "page"]


def _close_target(base: str, target_id: str) -> None:
    urllib.request.urlopen(f"{base}/json/close/{target_id}", timeout=5).read()


def _active_url(task_id: str) -> str | None:
    from tools import browser_tool as bt  # Hermes internals, imported lazily

    key = bt._last_session_key(task_id or "default")
    result = bt._session._run_browser_command(key, "get", ["url"], timeout=10)
    if not result.get("success"):
        return None
    return (result.get("data") or {}).get("url")


def _restart_supervisor(task_id: str, url: str) -> None:
    """Restart Hermes's CDP supervisor so it attaches to the page that is left.

    Re-pointing the running supervisor at the new tab (focus_page) is not
    enough: it keeps the frame tree it collected on the listing, so the next
    snapshot still reported the listing's URL (seen live 2026-09-28). A fresh
    supervisor attaches to the first page target, which after the close is
    the only one. Same helpers Hermes itself calls on every navigate.
    """
    from tools import browser_tool as bt
    from tools import browser_tool_cdp as cdp

    key = bt._last_session_key(task_id or "default")
    cdp._stop_cdp_supervisor(key)
    cdp._ensure_cdp_supervisor(key)
    logger.info("jobo tab keeper: supervisor restarted on %s", url)


def after_tool(tool_name: str, task_id: str) -> None:
    if tool_name not in TRIGGER_TOOLS:
        return
    source = os.environ.get("JOBO_SOURCE_URL", "").strip()
    base = _cdp_http_base()
    if not source or not base:
        return
    active = _active_url(task_id)
    if not active:
        return
    closing = tabs_to_close(_page_targets(base), active, source)
    for target_id in closing:
        _close_target(base, target_id)
        logger.info("jobo tab keeper: closed listing tab %s (agent is on %s)", target_id, active)
    if closing:
        _restart_supervisor(task_id, active)
