"""Open an ATS form that a careers page embeds in an iframe in the tab itself.

Stripe (2268) and Pantheon (2107) host their Greenhouse form in a cross-origin
iframe on their own careers page. The agent's snapshot and NEED_FIELDS read the
outer page only (2268's NEED_FIELDS listed one control: the locale picker), so
both agents hand-drove the form with raw CDP keyboard events and ran out of
steps with the dropdowns unanswered (2026-10-22 night run). The prompt already
says to open the iframe's URL once; 2119 (Abnormal) did, those two did not.

So the pipeline does it at the first upload or field-list request: if the
active page is not itself the ATS and holds an iframe of a known embeddable
ATS form, the tab navigates to the iframe's URL and the agent is told so.
Nothing is lost that the agent could have kept: inside the iframe it could not
see or reliably set fields, and the agent is asked to refill what it typed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Iframe URLs of ATS forms that work as a top-level page. Greenhouse's embed
# (job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>&validityToken=..)
# is matched on its path, Ashby (jobs.ashbyhq.com/<org>/<id>/...) on its host.
EMBED_FORM_RES = (
    re.compile(r"^https?://[^/]+/embed/job_app\?", re.I),
    re.compile(r"^https?://jobs\.ashbyhq\.com/[^?#]+", re.I),
)
# A page already on the ATS is the form (or its own listing), never a wrapper.
ATS_HOST_RE = re.compile(r"(?:^|\.)(?:greenhouse\.io|ashbyhq\.com)$", re.I)
LOAD_WAIT_SEC = 12.0

_IFRAME_SRCS_JS = (
    "JSON.stringify([...document.querySelectorAll('iframe')]"
    ".map(f => f.src || '').filter(Boolean))"
)


def embedded_form_url(top_url: str, iframe_urls: list[str]) -> str | None:
    """The iframe URL to open instead of `top_url`, if any."""
    host = urlparse(top_url or "").hostname or ""
    if not host or ATS_HOST_RE.search(host):
        return None
    for url in iframe_urls:
        if any(rx.search(url) for rx in EMBED_FORM_RES):
            return url
    return None


def promote(cdp_url: str) -> str:
    """Navigate the active tab to its embedded ATS form; a note for the agent, or ''."""
    from module4 import form_filler

    try:
        targets = form_filler._page_targets(cdp_url)
        if not targets:
            return ""
        tab = targets[0]
        with form_filler._Cdp(tab["webSocketDebuggerUrl"]) as cdp:
            r = cdp.call("Runtime.evaluate", {"expression": _IFRAME_SRCS_JS, "returnByValue": True})
            srcs = json.loads(r.get("result", {}).get("result", {}).get("value") or "[]")
            url = embedded_form_url(tab.get("url") or "", srcs)
            if not url:
                return ""
            outer = tab.get("url") or ""
            cdp.call("Page.navigate", {"url": url})
            deadline = time.monotonic() + LOAD_WAIT_SEC
            while time.monotonic() < deadline:
                time.sleep(0.5)
                state = cdp.call("Runtime.evaluate", {
                    "expression": "location.href + ' ' + document.readyState", "returnByValue": True})
                value = state.get("result", {}).get("result", {}).get("value") or ""
                if not value.startswith(("about:", outer)) and value.endswith("complete"):
                    break
            # React forms render after `complete`; give them a moment to mount.
            time.sleep(1.5)
    except Exception as e:
        logger.warning("embedded form promotion failed: %s", e)
        return ""
    host = urlparse(url).hostname or url
    logger.info("Opened the embedded %s form directly: %s", host, url[:160])
    return (
        f"[jobo: this careers page embeds its application form in an iframe from {host}, "
        "which your snapshot and NEED_FIELDS cannot see into. The pipeline opened that form "
        f"directly in this tab: {url}\n"
        "Fill it here: take a fresh browser_snapshot, re-enter anything you had typed into "
        "the iframe, and never open the careers page or this URL again (that reloads the "
        "form and throws away what is filled).]"
    )
