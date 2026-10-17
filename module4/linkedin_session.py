"""Is the apply browser still signed in to LinkedIn? (read-only checks)

Why this exists
---------------
2026-10-03 06:44:07: Humongo job 1005 (Paradox chat bot) wanted a "fresh chat"
and ran `browser_cdp Storage.clearCookies` on the browser's DEFAULT context.
That wiped every cookie in the user's signed-in apply profile, LinkedIn's `li_at`
session cookie included. Job 1007, a minute later, landed on LinkedIn as a
guest, tried to sign in itself (password-reset page, emailed PIN, then
LinkedIn's "Check your LinkedIn app" challenge), and every LinkedIn job after
it failed "LinkedIn requires sign-in" and was charged an attempt - 1140, 1173
and 1220 lost their last try. the user signed back in by hand at ~11:40.

A signed-out LinkedIn session is a PIPELINE problem, not a job problem: no
job can succeed until the user signs in again, and the agent must never try to.
This module answers the question cheaply so the runner can stop instead of
draining the queue.

Checks
------
* `check_session(cdp_url)` - one browser-level `Storage.getCookies` call over
  CDP, keeping only cookie NAME, DOMAIN and EXPIRY. Values are dropped the
  moment the response is parsed and are never logged, stored or returned.
  `li_at` present (and not expired) on .linkedin.com = signed in. Also flags a
  page target already sitting on a LinkedIn login/authwall/checkpoint URL.
  Opens no tab and sends nothing to LinkedIn.
* `probe_feed(cdp_url)` - opens linkedin.com/feed in a temporary tab and
  reports whether it bounced to /login, /authwall, /checkpoint or /uas/login.
  Catches a session LinkedIn revoked server-side while the cookie remained.
  Used only to confirm an agent's "LinkedIn requires sign-in" verdict.
* `is_signed_out_detail(text)` - recognises the agent's own wording for the
  same condition, so a job is never charged an attempt for it.

Nothing here signs in, types anything, or reads a cookie value.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# LinkedIn's authenticated-session cookie. Its presence on .linkedin.com is
# what the site itself keys "signed in" on; JSESSIONID/bcookie exist for
# guests too, so they prove nothing.
SESSION_COOKIE = "li_at"

# The verdict status the agent prints for this condition (module4/prompts.py).
SIGNED_OUT_STATUS = "linkedin_signed_out"

_AUTH_PATH_PREFIXES = (
    "/login",
    "/uas/",  # login, login-submit, request-password-reset (job 1007), logout
    "/authwall",
    "/checkpoint",
    "/signup",
    "/m/login",
    "/uas/logout",
    "/m/logout",
)

PROBE_URL = "https://www.linkedin.com/feed/"
PROBE_SETTLE_SEC = 5.0
PROBE_CLOSE_WAIT_SEC = 5.0


def enabled() -> bool:
    """`JOBO_LINKEDIN_SESSION_CHECK=0` turns the runner's check off."""
    return os.environ.get("JOBO_LINKEDIN_SESSION_CHECK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_linkedin_url(url: str) -> bool:
    host = _host(url)
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def is_linkedin_auth_url(url: str) -> bool:
    """A LinkedIn login / authwall / checkpoint / sign-out page."""
    if not is_linkedin_url(url):
        return False
    path = (urlsplit(url).path or "/").lower()
    return any(path.startswith(p) for p in _AUTH_PATH_PREFIXES)


# --- Agent wording -----------------------------------------------------------

# "LinkedIn requires sign-in", "LinkedIn requires signing in (no guest apply)",
# "LinkedIn login for <name>... passed the email OTP step", "redirects to
# LinkedIn's Join page", "LinkedIn job requires sign-in to view/apply".
# Proximity is required (no sentence break, <= 70 chars) so "Workday requires
# sign-in ... found via LinkedIn" style text does not match.
_SIGNED_OUT_WORDS = (
    r"sign[- ]?in\b|signing in|signed[- ]out|log[- ]?in\b|logging in|logged[- ]out|"
    r"join page|join now|join or sign in|join to apply|authwall|auth wall|login wall|"
    r"checkpoint|security (?:check|challenge|verification)|check your linkedin app|"
    r"guest (?:view|mode|session)|not signed in|not logged in"
)
_SIGNED_OUT_RE = re.compile(
    # "LinkedIn profile URL" / "LinkedIn link" is a form field, not the session.
    rf"linkedin(?!\W{{0,3}}(?:profile|url|link|handle|username))\W{{0,3}}(?:'s)?[^.;\n]{{0,70}}?(?:{_SIGNED_OUT_WORDS})"
    rf"|(?:sign[- ]?in|signing in|log[- ]?in|logging in)\s+(?:to|on|into|at)\s+(?:a\s+|the\s+)?linkedin",
    re.I,
)
# An ATS that merely OFFERS "LinkedIn" as one OAuth provider is a different
# wall (blockers._EXTERNAL_IDENTITY_ONLY), not our session.
_OAUTH_PROVIDER_LIST_RE = re.compile(
    r"only (?:offers|supports|allows|accepts|has)[^.;\n]{0,40}(?:oauth|sso|sign[- ]?up)", re.I
)


def is_signed_out_detail(text: str | None) -> bool:
    """Does an agent verdict say LinkedIn wants a sign-in?"""
    t = (text or "").strip()
    if not t:
        return False
    if t.lower().startswith(SIGNED_OUT_STATUS):
        return True
    if _OAUTH_PROVIDER_LIST_RE.search(t):
        return False
    return bool(_SIGNED_OUT_RE.search(t))


def is_signed_out_outcome(status: str | None, detail: str | None) -> bool:
    return (status or "").strip().lower() == SIGNED_OUT_STATUS or is_signed_out_detail(detail)


# --- Live checks -------------------------------------------------------------


@dataclass
class SessionState:
    state: str  # "signed_in" | "signed_out" | "unknown"
    reason: str = ""

    @property
    def signed_in(self) -> bool:
        return self.state == "signed_in"

    @property
    def signed_out(self) -> bool:
        return self.state == "signed_out"


def _http_json(url: str, *, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _browser_ws(cdp_url: str, timeout: float) -> str:
    info = _http_json(f"{cdp_url.rstrip('/')}/json/version", timeout=timeout)
    ws = info.get("webSocketDebuggerUrl") or ""
    if not ws:
        raise RuntimeError("CDP /json/version has no webSocketDebuggerUrl")
    return ws


def _cdp_call(ws_url: str, method: str, params: dict | None, timeout: float) -> dict:
    import websocket  # websocket-client, already a repo dependency

    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(f"{method}: no reply in {timeout}s")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def linkedin_cookie_names(cdp_url: str, *, timeout: float = 5.0) -> list[dict]:
    """Name/domain/expiry of every .linkedin.com cookie. Values are dropped here."""
    result = _cdp_call(_browser_ws(cdp_url, timeout), "Storage.getCookies", {}, timeout)
    out = []
    for c in result.get("cookies") or []:
        domain = str(c.get("domain") or "").lower().lstrip(".")
        if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
            out.append(
                {
                    "name": c.get("name"),
                    "domain": c.get("domain"),
                    "expires": c.get("expires"),
                    "session": c.get("session"),
                }
            )
    return out


def session_from_cookies(cookies: list[dict], *, now: float | None = None) -> SessionState:
    """Pure decision on (name, domain, expires) records. Testable offline."""
    now = time.time() if now is None else now
    hits = [c for c in cookies if c.get("name") == SESSION_COOKIE]
    if not hits:
        return SessionState("signed_out", f"no {SESSION_COOKIE} cookie on .linkedin.com")
    live = [
        c
        for c in hits
        if c.get("session") or not isinstance(c.get("expires"), (int, float))
        or c["expires"] <= 0 or c["expires"] > now
    ]
    if not live:
        return SessionState("signed_out", f"{SESSION_COOKIE} cookie has expired")
    return SessionState("signed_in", f"{SESSION_COOKIE} cookie present")


# Narrower than _AUTH_PATH_PREFIXES: only pages LinkedIn itself bounces a
# signed-out (or challenged) visitor to. A leftover tab is not evidence enough
# on a looser match.
_BOUNCE_PREFIXES = ("/login", "/uas/login", "/authwall", "/checkpoint")


def _is_bounced_tab(url: str) -> bool:
    if not is_linkedin_url(url):
        return False
    path = (urlsplit(url).path or "/").lower()
    return any(path.startswith(p) for p in _BOUNCE_PREFIXES)


def _page_urls(cdp_url: str, timeout: float) -> list[str]:
    tabs = _http_json(f"{cdp_url.rstrip('/')}/json/list", timeout=timeout)
    return [t.get("url", "") for t in tabs if t.get("type") == "page"]


def check_session(cdp_url: str, *, timeout: float = 5.0) -> SessionState:
    """Cheap read-only verdict: cookie presence plus any tab parked on a login page.

    "unknown" means the browser could not be asked (it is down or the call
    failed). The browser-health path owns that case; this function never
    turns a CDP hiccup into "signed out".
    """
    try:
        cookies = linkedin_cookie_names(cdp_url, timeout=timeout)
    except Exception as e:
        return SessionState("unknown", f"could not read cookies over CDP: {e}")
    state = session_from_cookies(cookies)
    if state.signed_out:
        return state
    try:
        stuck = [u for u in _page_urls(cdp_url, timeout) if _is_bounced_tab(u)]
    except Exception:
        stuck = []
    if stuck:
        return SessionState("signed_out", f"a tab is on a LinkedIn sign-in page: {stuck[0][:120]}")
    return state


def _page_ids(base: str, timeout: float) -> list[str]:
    tabs = _http_json(f"{base}/json/list", timeout=timeout)
    return [t.get("id") for t in tabs if t.get("type") == "page"]


def _close_and_wait(base: str, tab_id: str, timeout: float, wait_sec: float = PROBE_CLOSE_WAIT_SEC) -> None:
    """Close a tab and return only once /json/list no longer shows it.

    A CDP close is asynchronous. /json/list lists the newest tab first, so the
    runner's close_extra_tabs() right after an unwaited probe kept the dying
    probe tab, closed the real one, and Chromium exited with no page left:
    CDP 9333 (the LinkedIn browser) was relaunched 20 times in the 15 Oct
    12:40 run (GHO-17), each relaunch a risk to its cookies.
    """
    try:
        urllib.request.urlopen(f"{base}/json/close/{tab_id}", timeout=timeout).read()
    except Exception:
        return
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        try:
            if tab_id not in _page_ids(base, timeout):
                return
        except Exception:
            return
        time.sleep(0.2)


def probe_feed(cdp_url: str, *, settle_sec: float = PROBE_SETTLE_SEC, timeout: float = 5.0) -> SessionState:
    """Open linkedin.com/feed in a throwaway tab and see where it lands.

    Read-only: loads one page as whoever the browser already is, then closes
    that tab. Used to confirm an agent's "LinkedIn requires sign-in" claim,
    which also catches a session LinkedIn revoked while the cookie stayed.

    Never leaves the browser without a page: if the probe tab would be the
    only one, a blank guard tab is opened first, and the probe's close is
    waited out before returning (see _close_and_wait).
    """
    base = cdp_url.rstrip("/")
    try:
        if not _page_ids(base, timeout):
            _http_json(f"{base}/json/new?about:blank", method="PUT", timeout=timeout)
        tab = _http_json(f"{base}/json/new?{PROBE_URL}", method="PUT", timeout=timeout)
    except Exception as e:
        return SessionState("unknown", f"could not open a probe tab: {e}")
    tab_id = tab.get("id")
    try:
        url = ""
        deadline = time.monotonic() + settle_sec
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                tabs = _http_json(f"{base}/json/list", timeout=timeout)
            except Exception:
                continue
            cur = next((t for t in tabs if t.get("id") == tab_id), None)
            url = (cur or {}).get("url", "")
            if is_linkedin_auth_url(url):
                return SessionState("signed_out", f"linkedin.com/feed bounced to {url[:120]}")
        if url.startswith(PROBE_URL.rstrip("/")):
            return SessionState("signed_in", f"linkedin.com/feed stayed on {url[:120]}")
        return SessionState("unknown", f"probe landed on {url[:120] or '(nothing)'}")
    finally:
        _close_and_wait(base, tab_id, timeout)


def confirm_signed_out(cdp_url: str) -> SessionState:
    """Cookie check, then the feed probe if the cookie still looks fine."""
    state = check_session(cdp_url)
    if state.signed_out:
        return state
    probed = probe_feed(cdp_url)
    if probed.state != "unknown":
        return probed
    return state


# --- Sign-in / security senders the pipeline must never act for ------------

# The driver refuses OTP lookups and password resets for these: answering a
# LinkedIn or Google security challenge is signing in to the user's own account,
# which only the user does (job 1007, 2026-10-03, typed LinkedIn's emailed PIN).
_PROTECTED_DOMAINS = ("linkedin.com", "google.com", "accounts.google.com", "gmail.com")


def is_protected_account_domain(text: str | None) -> bool:
    """Does a sender hint / domain / From header name LinkedIn or Google?"""
    t = (text or "").lower()
    if not t:
        return False
    for m in re.finditer(r"[a-z0-9.-]+\.[a-z]{2,}", t):
        host = m.group(0).strip(".")
        if any(host == d or host.endswith("." + d) for d in _PROTECTED_DOMAINS):
            return True
    return bool(re.search(r"\blinkedin\b", t))
