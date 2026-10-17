"""Google Form applications that need a signed-in Google account.

What we measured (2026-10-03, scratch browser, signed out)
----------------------------------------------------------
Five jobs (233, 286, 841, 1368, 1379) ended with the agent reporting "Google
Form requires sign-in", and module4/blockers.py dropped them for good. The
three forms still on record (841 Siskin, 1368 Pipit Group, 1379 Corvid) were
opened signed out:

  * GET .../viewform answers HTTP 401 "You must sign in to access this
    content". A browser follows a 302 to accounts.google.com/v3/signin.
  * The page holds NO form data (no FB_PUBLIC_LOAD_DATA_), so there is no
    question list or entry id to work from.
  * Every variant is gated the same way: ?embedded=true, ?usp=sf_link,
    ?usp=pp_url, a mobile UA, formResponse (GET and POST).
  * Control: public application forms return 200 with FB_PUBLIC_LOAD_DATA_.

So the agent read these correctly. The form owner turned on a setting that
needs a Google identity (a file-upload question, "limit to 1 response" or
verified email collection; Google gives the same 401 for all three, so the
setting can't be read while signed out). No signed-out trick submits these
forms.

They are NOT unwinnable, though. One Google sign-in in the apply browser's
profile opens them. So the job is PARKED instead of dropped:

  * no attempt is charged and should_apply stays 1;
  * the form URL is recorded in module4_last_status;
  * each run skips a parked job WITHOUT spawning an agent, unless the apply
    browser is now signed in to Google (session cookie present) or the form
    has started answering 200 signed out (the owner changed the setting).

Detection never reads cookie values, only whether Google's session cookie
names exist.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

STATUS_PREFIX = "needs_google_signin:"

# docs.google.com/forms/d/e/<published id>/... or forms.gle short links.
# The scheme is optional: the agent often writes bare links in its DETAIL
# ("The Google Form (forms.gle/Ex4m... -> docs.google.com/forms/d/e/...)",
# job 1489); find_form_url() puts https:// back.
FORM_URL_RE = re.compile(
    r"(?:https?://)?docs\.google\.com/forms/(?:u/\d+/)?d/(?:e/)?[A-Za-z0-9_-]{20,}|"
    r"(?:https?://)?forms\.gle/[A-Za-z0-9]+",
    re.I,
)

# The agent's own words for this wall, taken verbatim from the five real
# DETAIL lines (see scripts/test_google_forms.py). Needs BOTH "google form"
# and a sign-in word close together: an ordinary ATS that merely offers
# "Continue with Google" must not be parked here.
_SIGNIN_WALL_RE = re.compile(
    r"google forms?\b.{0,140}?(?:sign(?:ed)?[- ]?in|log[- ]?in|google account)|"
    r"(?:sign[- ]?in|log[- ]?in).{0,80}?google forms?\b",
    re.I | re.S,
)

# Any link, with or without a scheme. Collapsed before _SIGNIN_WALL_RE runs so
# a long URL between "Google Form" and "sign-in" does not push them out of
# range: job 1489 (2026-10-11) put two links (~150 chars) in between and was
# dropped for good as "sign-in only via an external identity" instead of
# being parked.
_ANY_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*", re.I)

# Google's session cookies. Their presence (not their value) means the
# browser profile is signed in to a Google account.
_SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID"}

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)


def find_form_url(text: str | None) -> str | None:
    """First Google Form URL in free text, including inside a sign-in URL's
    ?continue= parameter (the page the agent is left on)."""
    t = text or ""
    for candidate in (t, urllib.parse.unquote(t), urllib.parse.unquote(urllib.parse.unquote(t))):
        m = FORM_URL_RE.search(candidate)
        if m:
            root = m.group(0)
            if not root.lower().startswith("http"):
                root = "https://" + root
            return root if "forms.gle" in root.lower() else root + "/viewform"
    return None


def is_signin_wall(detail: str | None, page_urls: list[str] | None = None) -> bool:
    """True when this failure is a Google Form sign-in wall.

    Either the agent said so (DETAIL), or the browser is sitting on Google's
    sign-in page with a Google Form as the continue target.
    """
    text = FORM_URL_RE.sub(" Google Form ", detail or "")
    if _SIGNIN_WALL_RE.search(_ANY_URL_RE.sub(" <link> ", text)):
        return True
    for u in page_urls or []:
        host = (urllib.parse.urlsplit(u).hostname or "").lower()
        if host == "accounts.google.com" and find_form_url(u):
            return True
    return False


def status_for(form_url: str | None, detail: str) -> str:
    """module4_last_status for a parked job: '<prefix><form url> | <detail>'."""
    return f"{STATUS_PREFIX}{form_url or 'unknown'} | {detail}"[:600]


def is_parked(last_status: str | None) -> bool:
    return (last_status or "").startswith(STATUS_PREFIX)


def parked_form_url(last_status: str | None) -> str | None:
    if not is_parked(last_status):
        return None
    head = (last_status or "")[len(STATUS_PREFIX):].split("|", 1)[0].strip()
    return head if head.startswith("http") else None


# ---------------------------------------------------------------------------
# Live checks (one HTTP GET / one CDP call; both fail safe)
# ---------------------------------------------------------------------------

OPEN = "open"  # answers 200 with the form's question data, signed out
SIGNIN_REQUIRED = "signin_required"  # 401 or redirect to accounts.google.com
UNKNOWN = "unknown"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - stdlib signature
        return None


@dataclass
class ProbeResult:
    state: str
    reason: str


def probe_form(url: str, *, timeout: float = 15.0) -> ProbeResult:
    """How a Google Form answers a SIGNED-OUT visitor."""
    if not url or not FORM_URL_RE.match(url):
        return ProbeResult(UNKNOWN, "not a Google Form URL")
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(400_000).decode("utf-8", "replace")
            code = r.status
            location = ""
    except urllib.error.HTTPError as e:
        code = e.code
        location = e.headers.get("Location") or ""
        body = ""
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return ProbeResult(UNKNOWN, f"network error: {e}")
    if code == 401:
        return ProbeResult(SIGNIN_REQUIRED, "HTTP 401: you must sign in to access this content")
    if code in (301, 302, 303, 307, 308) and "accounts.google.com" in location:
        return ProbeResult(SIGNIN_REQUIRED, f"HTTP {code} to Google sign-in")
    if code == 200 and "FB_PUBLIC_LOAD_DATA_" in body:
        return ProbeResult(OPEN, "HTTP 200 with form data")
    return ProbeResult(UNKNOWN, f"HTTP {code}")


def _browser_ws(cdp_url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=5) as r:
            return json.loads(r.read().decode()).get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001 - any failure means "can't tell"
        return None


def browser_signed_in(cdp_url: str) -> bool | None:
    """Whether the CDP browser's profile holds a Google session.

    True/False, or None when it can't be told (no browser, no websocket
    client). Reads cookie NAMES and domains only.
    """
    ws_url = _browser_ws(cdp_url)
    if not ws_url:
        return None
    try:
        import websocket  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
    except Exception as e:  # noqa: BLE001 - any connect failure means "can't tell"
        logger.warning("google sign-in check: CDP connect failed: %s", e)
        return None
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                break
        cookies = (msg.get("result") or {}).get("cookies") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("google sign-in check: Storage.getCookies failed: %s", e)
        return None
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    for c in cookies:
        domain = str(c.get("domain") or "").lower().lstrip(".")
        if c.get("name") in _SESSION_COOKIES and (domain == "google.com" or domain.endswith(".google.com")):
            return True
    return False


def page_urls(cdp_url: str) -> list[str]:
    """URLs of the browser's open page tabs (empty on any error)."""
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=5) as r:
            tabs = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 - any failure means "no tabs known"
        return []
    return [str(t.get("url") or "") for t in tabs if t.get("type") == "page"]


def release_reason(last_status: str | None, cdp_url: str) -> str | None:
    """Why a parked job may be attempted now, or None to keep it parked."""
    signed_in = browser_signed_in(cdp_url)
    if signed_in:
        return "apply browser is signed in to Google"
    form = parked_form_url(last_status)
    if form:
        pr = probe_form(form)
        if pr.state == OPEN:
            return f"form now opens signed out ({pr.reason})"
    return None
