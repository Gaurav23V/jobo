"""Route a LinkedIn listing to the worker that can apply to it (GHO-5).

Every queued job is a LinkedIn listing, but most applications happen on the
employer's own site (about 83% of submissions to mid-October 2026). Only the
LinkedIn worker's browser is signed in to LinkedIn, so it opens each listing
first, with no model involved, and decides:

  * external - LinkedIn's "Apply" button opens the employer's form. The form
    URL is captured from the tab that opens, and the job goes to a form
    worker, whose browser has never seen LinkedIn.
  * easy_apply - the LinkedIn Easy Apply modal; only the signed-in browser
    can do it, so the LinkedIn worker keeps it.
  * closed - LinkedIn says the posting no longer accepts applications.
  * unknown - anything else (no apply button found, sign-in wall, the click
    opened nothing, a timeout). The LinkedIn worker then applies to it the
    way the single-worker runner always has, so a routing failure costs
    throughput, never an application.

This is the same thing the apply agent does first on every job (open the
listing, click Apply), minus the model. Listing opens are paced
(min_interval) so the signed-in account does not browse faster than a person
would.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

EASY_APPLY = "easy_apply"
EXTERNAL = "external"
CLOSED = "closed"
UNKNOWN = "unknown"

_LINKEDIN_HOSTS = ("linkedin.com", "licdn.com", "lnkd.in")

# LinkedIn's closed-posting banner. "Not currently accepting applications" is
# the wording since October 2026 (GHO-24); before that it was "No longer
# accepting applications". The same pattern is used inside _CLASSIFY_JS.
_CLOSED_PATTERN = r"(?:no longer|not currently) accepting applications"
_CLOSED_RE = re.compile(_CLOSED_PATTERN, re.I)

# Runs inside the listing page. Returns what the top card offers.
_CLASSIFY_JS = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const label = e => ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')).replace(/\s+/g, ' ').trim();
  const out = {href: location.href, ready: document.readyState};
  const main = document.querySelector('main') || document.body;
  const closed = (main ? main.innerText : '').match(/__CLOSED__/i);
  out.closed = closed ? closed[0] : '';
  const cands = [...document.querySelectorAll('button, a')].filter(e => vis(e) && /\bapply\b/i.test(label(e)));
  const easy = cands.find(e => /easy apply/i.test(label(e)));
  const ext = cands.find(e => !/easy apply/i.test(label(e)) && (
      /company website|on .*website|apply on/i.test(label(e)) ||
      /jobs-apply-button/.test(e.className || '') || /^apply$/i.test((e.innerText || '').trim())));
  out.easy = !!easy;
  if (ext) {
    ext.scrollIntoView({block: 'center'});
    const r = ext.getBoundingClientRect();
    out.ext = {x: r.left + r.width / 2, y: r.top + r.height / 2, label: label(ext).slice(0, 120),
               href: ext.tagName === 'A' ? ext.href : ''};
  }
  out.authwall = /\/(authwall|login|checkpoint|signup)/.test(location.pathname);
  return JSON.stringify(out);
})()
""".replace("__CLOSED__", _CLOSED_PATTERN)

# A safety-reminder / share-profile interstitial some external jobs show
# before the employer's tab opens.
_CONTINUE_JS = r"""
(() => {
  const dlg = document.querySelector('[role=dialog], .artdeco-modal');
  if (!dlg) return '';
  const b = [...dlg.querySelectorAll('button, a')].find(e => /^(continue|continue applying|continue to apply)$/i.test((e.innerText || '').trim()));
  if (!b) return '';
  const r = b.getBoundingClientRect();
  return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
})()
"""


@dataclass
class Route:
    kind: str
    apply_url: str = ""
    reason: str = ""


def is_linkedin_host(url: str) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _LINKEDIN_HOSTS)


def _usable_external(url: str) -> bool:
    try:
        p = urlsplit(url or "")
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname) and not is_linkedin_host(url)


def min_interval() -> float:
    raw = os.environ.get("JOBO_ROUTE_MIN_INTERVAL_SEC", "").strip()
    return float(raw) if raw.replace(".", "", 1).isdigit() else 15.0


_last_open = 0.0


def _pace() -> None:
    """Space LinkedIn listing opens: min_interval() plus 0-5s of jitter."""
    global _last_open
    gap = min_interval()
    wait = _last_open + gap + (random.uniform(0, 5) if gap else 0) - time.monotonic()
    if _last_open and wait > 0:
        time.sleep(wait)
    _last_open = time.monotonic()


class _Page:
    """Minimal CDP session on one page target."""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        import websocket  # websocket-client, already a repo dependency

        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self.n = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self.n += 1
        my_id = self.n
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(f"{method}: no reply in {timeout}s")

    def eval_json(self, expression: str) -> dict | None:
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        value = (res.get("result") or {}).get("value")
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    def click(self, x: float, y: float) -> None:
        # A trusted mouse click: LinkedIn opens the employer's tab from a user
        # gesture, which a JS element.click() is not.
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            if kind == "mouseMoved":
                params = {"type": kind, "x": x, "y": y}
            self.call("Input.dispatchMouseEvent", params)
            time.sleep(0.05)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _http(cdp_url: str, path: str, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(f"{cdp_url.rstrip('/')}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


def _pages(cdp_url: str) -> list[dict]:
    try:
        return [t for t in _http(cdp_url, "/json/list") if t.get("type") == "page"]
    except Exception:
        return []


def _close_target(cdp_url: str, target_id: str) -> None:
    try:
        _http(cdp_url, f"/json/close/{target_id}")
    except Exception:
        pass


def _wait_gone(cdp_url: str, target_ids: list[str], timeout: float = 5.0) -> None:
    """Closing a tab over CDP is asynchronous: the target stays listed for a
    moment. Anything that picks "the tab to keep" from /json/list right after
    a close can pick the dying one and close the live one, which leaves
    Chromium with no tabs, and it exits. That killed the LinkedIn browser on
    2026-10-14 (GHO-5 smoke test). So every close here waits until it is real."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = {t.get("id") for t in _pages(cdp_url)}
        if not any(t in live for t in target_ids):
            return
        time.sleep(0.2)


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlsplit(a or ""), urlsplit(b or "")
    return (pa.hostname, pa.path.rstrip("/")) == (pb.hostname, pb.path.rstrip("/"))


def _await_external(cdp_url: str, before: set[str], page_id: str, page: _Page, timeout: float,
                    listing_url: str = "") -> tuple[str, list[str]]:
    """Wait for the employer's page after the Apply click. Returns (url, ids of
    new tabs to close). The URL is taken once it has left LinkedIn's redirect
    hops and stopped changing."""
    deadline = time.monotonic() + timeout
    last, stable_since, new_ids = "", 0.0, []
    continued = False
    while time.monotonic() < deadline:
        pages = _pages(cdp_url)
        new = [t for t in pages if t.get("id") not in before]
        new_ids = [t["id"] for t in new]
        candidates = [t.get("url", "") for t in new]
        same = next((t.get("url", "") for t in pages if t.get("id") == page_id), "")
        if same and not is_linkedin_host(same) and same.startswith("http") and not _same_page(same, listing_url):
            candidates.append(same)  # the click navigated the listing tab itself
        url = next((u for u in candidates if _usable_external(u)), "")
        if url:
            if url != last:
                last, stable_since = url, time.monotonic()
            elif time.monotonic() - stable_since >= 1.5:
                return url, new_ids
        elif not new and not continued and time.monotonic() > deadline - timeout + 3:
            try:
                btn = page.eval_json(_CONTINUE_JS)
            except Exception:
                btn = None
            if btn:
                page.click(btn["x"], btn["y"])
            continued = True
        time.sleep(0.5)
    return last, new_ids


def resolve(cdp_url: str, listing_url: str, *, load_timeout: float = 20.0, open_timeout: float = 15.0,
            _any_host: bool = False) -> Route:
    """Open the listing in the signed-in browser and decide its route. Never raises.

    Works in the browser's existing first tab and never closes it: only the
    tab(s) the Apply click opened are closed, and only after the listing tab
    is known to be alive, so the browser always keeps a page. The listing tab
    is left on about:blank, so the next agent never mistakes this listing
    for its own job. (_any_host lets the regression test use a local page.)
    """
    if not _any_host and not is_linkedin_host(listing_url):
        if _usable_external(listing_url):
            return Route(EXTERNAL, listing_url, "listing is already the employer's page")
        return Route(UNKNOWN, reason="not a LinkedIn listing")
    _pace()
    page = None
    tab_id = ""
    opened: list[str] = []
    try:
        pages = _pages(cdp_url)
        if not pages:
            return Route(UNKNOWN, reason="browser has no page to route in")
        tab_id = pages[0]["id"]
        before = {t.get("id") for t in pages}
        page = _Page(pages[0]["webSocketDebuggerUrl"])
        page.call("Page.enable")
        page.call("Page.navigate", {"url": listing_url})
        deadline = time.monotonic() + load_timeout
        info: dict | None = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            info = page.eval_json(_CLASSIFY_JS)
            if not info:
                continue
            if info.get("authwall"):
                return Route(UNKNOWN, reason=f"LinkedIn showed a sign-in page ({info.get('href', '')[:80]})")
            if info.get("closed"):
                return Route(CLOSED, reason=f'LinkedIn says "{info["closed"]}"')
            if info.get("easy"):
                return Route(EASY_APPLY, reason="Easy Apply button")
            if info.get("ext") and info.get("ready") == "complete":
                break
        if not info or not info.get("ext"):
            return Route(UNKNOWN, reason="no Apply button found on the listing")
        ext = info["ext"]
        if ext.get("href") and _usable_external(ext["href"]) and not _any_host:
            return Route(EXTERNAL, ext["href"], f"Apply link ({ext.get('label', '')[:60]})")
        page.click(ext["x"], ext["y"])
        url, opened = _await_external(cdp_url, before, tab_id, page, open_timeout, listing_url)
        if url:
            return Route(EXTERNAL, url, f"Apply opened {urlsplit(url).hostname}")
        return Route(UNKNOWN, reason="Apply click opened no employer page")
    except Exception as e:  # routing must never break the run
        logger.warning("linkedin_router: could not route %s: %s", listing_url, e)
        return Route(UNKNOWN, reason=f"routing error: {str(e)[:120]}")
    finally:
        if page is not None:
            try:
                page.call("Page.navigate", {"url": "about:blank"}, timeout=5)
            except Exception:
                pass
            page.close()
        alive = any(t.get("id") == tab_id for t in _pages(cdp_url)) if tab_id else False
        closing = [t for t in opened if t != tab_id]
        if alive and closing:
            for target_id in closing:
                _close_target(cdp_url, target_id)
            _wait_gone(cdp_url, closing)
