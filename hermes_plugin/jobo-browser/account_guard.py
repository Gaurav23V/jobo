"""Keep the apply agent away from the user's own LinkedIn/Google sessions and cookies.

What happened (2026-10-03, run_20261003_0637):
  06:44:07  Humongo job 1005, stuck in a Paradox chat with an expired link, ran
            `browser_cdp Storage.clearCookies {browserContextId: <default>}`
            to "start a fresh chat". The apply browser is ONE shared profile,
            so that wiped every cookie in it, LinkedIn's session included.
  06:45:52  Job 1007 opened LinkedIn as a guest ("Sign in to see who you already
            know at Vanarsdel"), clicked "Forgot password?", typed the user's email,
            asked the pipeline for LinkedIn's emailed PIN (NEED_OTP linkedin.com),
            typed it, and stopped at LinkedIn's "Check your LinkedIn app" step.
  06:49-07:27 every LinkedIn job: "LinkedIn requires sign-in", attempt charged.

The prompt alone cannot be the guard: the agent picked the cookie wipe as a
"genuinely different angle" after a retry notice told it not to repeat itself.
This module is a `pre_tool_call` veto (Hermes turns the message into the tool
result), so the destructive call never reaches the browser:

  * browser_cdp: no cookie/storage wipes of the shared profile, no cookie
    writes for LinkedIn/Google, no closing the browser.
  * browser_console: no script that clears cookies or storage.
  * browser_navigate: no LinkedIn/Google sign-in, sign-out, password-reset or
    checkpoint pages.
  * Once a navigate/snapshot result shows a LinkedIn sign-in page or LinkedIn's
    signed-out guest view, clicks/typing/console on that page are refused and
    the agent is told to report STATUS: linkedin_signed_out.
  * browser_type: a password-shaped value that is not the pipeline's standard
    account password is refused (Wingtip job 818 typed three invented passwords).
    On a host listed in secrets.json `site_passwords` the expected password is
    the one recorded there (Wingtip 929 set a variant on 2026-09-18 that the
    portal will not let us change back).

Everything fails OPEN on unexpected input (a guard bug must not break every
tool call), but every rule that matches fails CLOSED.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import os
from pathlib import Path


def _load_secrets() -> dict:
    """secrets.json from the candidate's local config.

    Same lookup as policy/personal.py (asserted equal by
    scripts/test_linkedin_session.py): JOBO_PERSONAL_DIR or <repo>/personal/.
    The plugin runs inside the Hermes process, which cannot import the jobo
    package, so the repo is found from this file's real (symlink-resolved)
    location.
    """
    repo = Path(__file__).resolve().parents[2]
    raw = os.environ.get("JOBO_PERSONAL_DIR", "").strip()
    if raw:
        pdir = Path(raw).expanduser()
        pdir = pdir if pdir.is_absolute() else repo / pdir
    elif (repo / "personal").is_dir():
        pdir = repo / "personal"
    else:
        pdir = repo / "personal.example"
    try:
        data = json.loads((pdir / "secrets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_standard_password() -> str:
    """The pipeline's one job-site password: JOBO_ACCOUNT_PASSWORD, else secrets.json."""
    return os.environ.get("JOBO_ACCOUNT_PASSWORD", "") or str(
        _load_secrets().get("account_password") or ""
    )


def _load_site_passwords() -> dict[str, str]:
    """Same as policy.personal.site_passwords()."""
    raw = _load_secrets().get("site_passwords")
    if not isinstance(raw, dict):
        return {}
    return {
        str(host).lower().strip(" ."): str(pw)
        for host, pw in raw.items()
        if "." in str(host).strip(" .") and pw
    }


# Keep equal to module4.password_reset.STANDARD_PASSWORD. Never log it.
STANDARD_PASSWORD = _load_standard_password()
SITE_PASSWORDS = _load_site_passwords()

SIGNED_OUT_STATUS = "linkedin_signed_out"

_PROTECTED_SUFFIXES = ("linkedin.com", "google.com", "gmail.com", "youtube.com")

# CDP methods that wipe or rewrite the shared profile's cookies/storage, or
# take the shared browser down. Matched case-insensitively.
_ALWAYS_BLOCKED_CDP = {
    "storage.clearcookies",
    "network.clearbrowsercookies",
    "browser.close",
    "browser.crash",
    "target.disposebrowsercontext",
}
# Allowed only when every origin/domain they name is outside LinkedIn/Google.
_SCOPED_CDP = {
    "storage.cleardatafororigin",
    "storage.cleardataforstoragekey",
    "network.deletecookies",
    "network.setcookie",
    "network.setcookies",
    "storage.setcookies",
}

_CONSOLE_WIPE_RE = re.compile(
    r"clearcookies|clearbrowsercookies|deletecookies|document\.cookie\s*=|cookiestore\.delete|"
    r"(?:localstorage|sessionstorage)\.clear\s*\(|indexeddb\.deletedatabase|caches\.delete",
    re.I,
)

_LINKEDIN_AUTH_PATHS = (
    "/login",
    "/uas/",
    "/authwall",
    "/checkpoint",
    "/signup",
    "/m/login",
    "/m/logout",
    "/logout",
    "/psettings/sessions",
)
_GOOGLE_AUTH_HOSTS = ("accounts.google.com",)

# LinkedIn's signed-out (guest) job page. Seen verbatim in job 1007-1220
# snapshots; a signed-in page never shows these.
_GUEST_MARKERS = (
    "sign in to see who you already know",
    "join or sign in to find your next job",
    "join to apply for the",
    "sign in to view more jobs",
    "welcome back",
    "new to linkedin? join now",
    'link "join now"',
    'button "join now"',
)

PASSWORD_SHAPE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[^A-Za-z0-9\s])\S{8,64}$")

_state = {"linkedin_signed_out": False, "where": "", "host": ""}


def reset_state() -> None:
    _state["linkedin_signed_out"] = False
    _state["where"] = ""
    _state["host"] = ""


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _is_protected_host(host: str) -> bool:
    host = (host or "").lower().lstrip(".")
    return any(host == s or host.endswith("." + s) for s in _PROTECTED_SUFFIXES)


def is_linkedin_url(url: str) -> bool:
    h = _host(url)
    return h == "linkedin.com" or h.endswith(".linkedin.com")


def is_account_auth_url(url: str) -> bool:
    """LinkedIn or Google sign-in / sign-out / reset / checkpoint page."""
    h = _host(url)
    if h in _GOOGLE_AUTH_HOSTS:
        return True
    if h == "linkedin.com" or h.endswith(".linkedin.com"):
        path = (urlsplit(url).path or "/").lower()
        return any(path.startswith(p) for p in _LINKEDIN_AUTH_PATHS)
    return False


def _scope_hosts(params: dict) -> list[str]:
    hosts = []
    for key in ("origin", "url", "domain", "storageKey"):
        v = params.get(key)
        if isinstance(v, str) and v:
            hosts.append(_host(v) or v.lower().lstrip("."))
    for c in params.get("cookies") or []:
        if isinstance(c, dict):
            for key in ("domain", "url"):
                v = c.get(key)
                if isinstance(v, str) and v:
                    hosts.append(_host(v) or v.lower().lstrip("."))
    return hosts


def _msg_cookie_wipe(what: str) -> str:
    return (
        f"BLOCKED by jobo: {what}. This browser is one shared profile that holds "
        "the user's LinkedIn and Google sign-ins; clearing or rewriting its cookies or "
        "storage signs him out of everything (this exact call signed LinkedIn out "
        "mid-run on 2026-10-03 and cost 4 jobs). Never clear cookies, site data or "
        "storage. If a site only works with a fresh session, report STATUS: stuck "
        "with that reason instead."
    )


def _msg_signed_out(where: str) -> str:
    return (
        f"BLOCKED by jobo: this is a LinkedIn/Google sign-in page ({where[:120]}). "
        "You must never sign in, sign out, reset a password or answer a security "
        "check for LinkedIn or Google - that is the user's own account and only he signs "
        "in. If LinkedIn is showing you as signed out, stop now and end with:\n"
        f"STATUS: {SIGNED_OUT_STATUS}\n"
        "DETAIL: LinkedIn shows the apply browser as signed out\n"
        "FIELDS_FILLED_JSON: {}\n"
        "The pipeline then stops the run without charging this job an attempt."
    )


def site_password(host: str) -> str:
    """Same match as policy.personal.site_password()."""
    host = (host or "").lower().strip(" .")
    if "." not in host:
        return ""
    for site, pw in SITE_PASSWORDS.items():
        if host == site or host.endswith("." + site) or site.endswith("." + host):
            return pw
    return ""


def expected_password() -> str:
    """The password a password field on the current page should get."""
    return site_password(_state["host"]) or STANDARD_PASSWORD


def _known_password(text: str) -> bool:
    """`text` is the password this page expects. Each handoff turn is a fresh
    Hermes process, so before its first navigate/snapshot the host is unknown
    and any recorded site password counts as known too."""
    if not text:
        return False
    if text == expected_password():
        return True
    return not _state["host"] and text in SITE_PASSWORDS.values()


def _msg_password() -> str:
    if site_password(_state["host"]):
        return (
            f"BLOCKED by jobo: the account on {_state['host']} already exists and an "
            f"earlier run set its password to {expected_password()}. Sign in with "
            f"exactly that password; do not invent another or reset it."
        )
    return (
        "BLOCKED by jobo: that looks like a made-up password. Account passwords are "
        f"never invented: type exactly {STANDARD_PASSWORD} in BOTH the password and "
        "confirm-password fields (it is the one password this pipeline uses, so a "
        "later run can sign in). If the portal's rules reject it, report "
        "STATUS: stuck quoting the rule - do not try another password. If this was "
        "not a password field, type the value from the candidate facts instead."
    )


def check_tool_call(tool_name: str, args: dict | None) -> str | None:
    """Return a block message, or None to let the call through."""
    a = args if isinstance(args, dict) else {}
    name = tool_name or ""

    if name == "browser_cdp":
        method = str(a.get("method") or "").strip()
        low = method.lower()
        raw_params = a.get("params")
        params: dict = raw_params if isinstance(raw_params, dict) else {}
        if low in _ALWAYS_BLOCKED_CDP:
            return _msg_cookie_wipe(f"browser_cdp {method}")
        if low in _SCOPED_CDP:
            hosts = _scope_hosts(params)
            if not hosts or any(_is_protected_host(h) for h in hosts):
                return _msg_cookie_wipe(
                    f"browser_cdp {method} " + ("without a site scope" if not hosts
                                                 else f"on {', '.join(hosts)}")
                )
        if low in ("page.navigate", "target.createtarget"):
            url = str(params.get("url") or "")
            if is_account_auth_url(url):
                return _msg_signed_out(url)
        return None

    if name == "browser_console":
        expr = str(a.get("expression") or "")
        if expr and _CONSOLE_WIPE_RE.search(expr):
            return _msg_cookie_wipe("a script that clears cookies or storage")
        if expr and _state["linkedin_signed_out"]:
            return _msg_signed_out(_state["where"])
        return None

    if name == "browser_navigate":
        url = str(a.get("url") or "")
        if is_account_auth_url(url):
            return _msg_signed_out(url)
        return None

    if name in ("browser_click", "browser_type", "browser_press") and _state["linkedin_signed_out"]:
        return _msg_signed_out(_state["where"])

    if name == "browser_type":
        text = str(a.get("text") or "")
        if text == STANDARD_PASSWORD or _known_password(text) or is_password_ref(a.get("ref")):
            return None  # a known password field is rewritten by password_rewrite()
        if _looks_invented_password(text):
            return _msg_password()
    return None


# --- Account passwords --------------------------------------------------------

# ref -> accessible name from the latest snapshot, so a browser_type can be
# tied to the field it targets ("Password *", "Confirm Password").
_REF_NAMES: dict[str, str] = {}
_REF_RE = re.compile(r'^\s*-\s*(\w+)\s+"([^"]*)"[^\n]*?\[[^\]]*\bref=(e\d+)', re.M)
_PASSWORD_LABEL_RE = re.compile(r"pass\s*word|passcode|\bpwd\b", re.I)


def remember_refs(snapshot: str) -> None:
    for role, label, ref in _REF_RE.findall(snapshot or ""):
        _REF_NAMES[ref] = f"{role} {label}"


def is_password_ref(ref) -> bool:
    name = _REF_NAMES.get(str(ref or "").lstrip("@"), "")
    return bool(name) and bool(_PASSWORD_LABEL_RE.search(name)) and not re.search(
        r"forgot|reset|show|hide|strength", name, re.I
    )


def _looks_invented_password(text: str) -> bool:
    """Unknown field, but the value has every hallmark of a made-up password.

    Upper, lower, digit and symbol, 10+ chars, no spaces, not an email or URL:
    "Alex@Zeno2026!", "AlexE2026!Secure". Candidate facts never look like
    this (dates, phone numbers and CTC values all fail one test).
    """
    if not PASSWORD_SHAPE.match(text) or len(text) < 10:
        return False
    if re.search(r"https?://|www\.|@[\w-]+\.[a-z]{2,}", text, re.I):
        return False
    # Password-style symbols only: "Python3.11" or "Node.js-18" are skills.
    return bool(
        re.search(r"[A-Z]", text) and re.search(r"[a-z]", text) and re.search(r"[!@#$%^&*?]", text)
    )


def password_rewrite(tool_name: str, args: dict | None) -> dict | None:
    """Args override that puts the standard password into a password field."""
    a = args if isinstance(args, dict) else {}
    if tool_name != "browser_type" or not is_password_ref(a.get("ref")):
        return None
    want = expected_password()
    if not want or _known_password(str(a.get("text") or "")):
        return None
    return {"text": want}


def _result_url(data: dict) -> str:
    url = data.get("url")
    if isinstance(url, str) and url:
        return url
    tree = data.get("frame_tree")
    top = tree.get("top") if isinstance(tree, dict) else None
    return str(top.get("url") or "") if isinstance(top, dict) else ""


def observe_result(tool_name: str, result) -> str | None:
    """Track whether the agent is looking at a signed-out LinkedIn page.

    Returns a notice to prepend to the snapshot when it just became signed
    out, else None. Called from transform_tool_result.
    """
    if tool_name not in ("browser_navigate", "browser_snapshot") or not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw_snap = str(data.get("snapshot") or "")
    remember_refs(raw_snap)
    url = _result_url(data)
    if not url:
        return None
    if not is_linkedin_url(url) and _host(url):
        _state["host"] = _host(url)
    snap = raw_snap.lower()
    if is_account_auth_url(url) or (
        is_linkedin_url(url) and any(m in snap for m in _GUEST_MARKERS)
    ):
        _state["linkedin_signed_out"] = True
        _state["where"] = url
        return (
            "[jobo: LinkedIn is showing this browser as SIGNED OUT (or this is a "
            "LinkedIn/Google sign-in page). Do not dismiss it, do not sign in. End now "
            f"with STATUS: {SIGNED_OUT_STATUS}.]"
        )
    if not is_linkedin_url(url):
        # On the employer's own site: lift the restriction so the agent is not
        # blocked on a real form. While still on LinkedIn the flag is STICKY:
        # job 1007's later snapshots, taken after it dismissed the guest modal,
        # carried no guest text at all (10 of 37 signed-out results).
        _state["linkedin_signed_out"] = False
        _state["where"] = ""
    return None


def annotate(tool_name: str, result):
    """transform_tool_result helper: prepend the signed-out notice, or None."""
    notice = observe_result(tool_name, result)
    if not notice:
        return None
    try:
        data = json.loads(result)
        data["snapshot"] = notice + "\n" + str(data.get("snapshot") or "")
        data["jobo_linkedin_signed_out"] = True
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return None
