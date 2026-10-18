"""Bot walls the apply agent cannot pass, and the click-tracker hops behind them.

Measured 2026-10-11 in scratch browsers (never the jobo profile):

  * DataDome ("Verification Required" / "You have been blocked", iframe from
    geo.captcha-delivery.com, "Slide right to secure your access"). A
    human-like slide gave "Access is temporarily restricted"; the AUDIO route
    (whisper read all 6 digits correctly) gave the same. DataDome scores the
    browser and network, not the answer, so solving it is pointless.
  * Every Kestrelway wall sat on click.appcast.io, a job-board click TRACKER that
    redirects to the employer's job page. It is not the form. In 4 of 4 fresh
    profiles the first visit was walled and a plain second visit 5 s later
    redirected straight to careers.kestrelway.example, whose own Apply link goes
    to a Workday form with no captcha. Kestrelway 1505 instead navigated back to
    the tracker a third time after already reaching Kestrelway's page and then
    spent 3.5 min on the wall; 1477 gave up on the first wall.

So:
  * A DataDome wall reached through a tracker gets ONE more plain visit to the
    tracker (it usually redirects), then the employer's own Apply link.
  * A DataDome wall anywhere else, or a second wall: STATUS captcha_failed now.
  * Repeat visits to a tracker (beyond MAX_TRACKER_VISITS per job) and direct
    navigation to captcha-delivery.com are blocked; so are screenshots and
    synthetic mouse input while the current page is a DataDome wall.

On-page jigsaw sliders (Redpine's canvas "Slide to verify") are different: the
page itself checks the drag. Those go to the NEED_SLIDER handoff
(module4/slider_captcha.py), and the notice here says so.

A VISIBLE hCaptcha challenge (an 'Iframe "hCaptcha challenge"' with its Skip /
Refresh buttons; the invisible widget alone shows neither) has never been
solved by the agent: 1878 and 1934 (2026-10-18) spent 15 and 17 model steps on
tiles and drags first. It ends the run at once. On Oracle Cloud and iCIMS the
challenge gates the email step for every visitor (module4/captcha_walls.py),
so there it ends as STATUS: captcha_wall and the job is parked, not charged.

State is per Hermes process (one agent run; a NEED_* handoff starts a new
process, which is fine: the counters only need to stop loops inside a run).
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

# Click trackers / programmatic job-ad redirectors. They hold no form.
TRACKER_HOSTS = (
    "click.appcast.io",
    "apply.appcast.io",
    "click.margiex.com",
    "click.pandologic.com",
)
MAX_TRACKER_VISITS = 2

_DATADOME_HOST = re.compile(r"(^|\.)captcha-delivery\.com$", re.I)
_DATADOME_URL = re.compile(r"captcha-delivery\.com", re.I)
_DATADOME_TEXT = re.compile(
    r"datadome captcha|slide right to secure your access|"
    r"you have been blocked \| [^\"\n]*verification|access is temporarily restricted",
    re.I,
)
_JIGSAW_TEXT = re.compile(r"\bslide to verify\b", re.I)
_HCAPTCHA_FRAME = re.compile(r'^\s*- Iframe "hCaptcha challenge"', re.I | re.M)
_HCAPTCHA_CONTROLS = re.compile(
    r'^\s*- button "(?:Skip Challenge|Refresh Challenge|Challenge refreshed)', re.I | re.M
)

# Same list as module4/captcha_walls.GATE_HOST_SUFFIXES (this plugin cannot
# import module4); scripts/test_hcaptcha_walls.py keeps the two equal.
GATE_HOST_SUFFIXES = ("oraclecloud.com", "icims.com")

CAPTCHA_FAILED_DETAIL = "DataDome bot wall (geo.captcha-delivery.com); cannot be passed by an agent"

_state: dict = {"datadome": False, "hcaptcha": False, "tracker_visits": 0, "walls": 0, "url": ""}


def reset_state() -> None:
    _state.update(datadome=False, hcaptcha=False, tracker_visits=0, walls=0, url="")


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def is_tracker_url(url: str) -> bool:
    h = _host(url)
    return any(h == t or h.endswith("." + t) for t in TRACKER_HOSTS)


def is_gate_host(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in GATE_HOST_SUFFIXES)


def _page_url(data: dict) -> str:
    """The page a result shows: its url, else the frame tree's top frame."""
    url = data.get("url")
    if isinstance(url, str) and url:
        return url
    tree = data.get("frame_tree")
    top = tree.get("top") if isinstance(tree, dict) else None
    return str(top.get("url") or "") if isinstance(top, dict) else ""


def _nav_url(tool_name: str, a: dict) -> tuple[str, str]:
    """(url, cdp_method) of a navigation call, else ('', method)."""
    if tool_name == "browser_navigate":
        return str(a.get("url") or ""), ""
    if tool_name == "browser_cdp":
        method = str(a.get("method") or "").strip().lower()
        raw = a.get("params")
        params: dict = raw if isinstance(raw, dict) else {}
        if method in ("page.navigate", "target.createtarget"):
            return str(params.get("url") or ""), method
        return "", method
    return "", ""


def check_tool_call(tool_name: str, args: dict | None) -> str | None:
    a = args if isinstance(args, dict) else {}
    url, method = _nav_url(tool_name, a)
    if _state["hcaptcha"] and (tool_name == "browser_vision" or method.startswith("input.dispatch")):
        return (
            "BLOCKED by jobo: an hCaptcha image challenge is on screen. The agent has never "
            "solved one, and every attempt spent 10+ model steps on it. Do not study, click "
            "or drag it. End now as the page notice says."
        )
    if _state["datadome"] and (tool_name == "browser_vision" or method.startswith("input.dispatch")):
        return (
            "BLOCKED by jobo: the current page is a DataDome bot wall. Screenshots, drags "
            "and scripted input cannot pass it (tested: slide and audio both rejected). "
            "Follow the instruction in the page notice: one more visit to the tracker URL "
            "if it came from one, otherwise end now with STATUS: captcha_failed."
        )
    if not url:
        return None
    if _DATADOME_HOST.search(_host(url)):
        return (
            "BLOCKED by jobo: captcha-delivery.com is DataDome's bot wall itself. It cannot "
            "be passed. End now with STATUS: captcha_failed, or use the employer's own "
            "careers page if one is already open."
        )
    if is_tracker_url(url):
        if _state["tracker_visits"] >= MAX_TRACKER_VISITS:
            return (
                f"BLOCKED by jobo: {_host(url)} is a job-board click tracker, not the form, "
                f"and it has been opened {_state['tracker_visits']} times this run. More visits "
                "trigger a DataDome wall (Kestrelway 1505). If the employer's own job page is open "
                "in a tab, use ITS Apply / Apply Now link (e.g. a *.myworkdayjobs.com form). "
                "Otherwise end with STATUS: captcha_failed and DETAIL naming the tracker."
            )
        _state["tracker_visits"] += 1
    return None


def _frames_text(data: dict) -> str:
    tree = data.get("frame_tree")
    try:
        return json.dumps(tree) if tree else ""
    except (TypeError, ValueError):
        return ""


def wall_tracker(data: dict) -> str:
    """The click-tracker URL a DataDome wall came from, or ''."""
    url = str(data.get("url") or "")
    if is_tracker_url(url):
        return url
    for m in re.finditer(r"https?://[^\s\"'\\]*captcha-delivery\.com[^\s\"'\\]*", _frames_text(data) + " " + url):
        try:
            ref = parse_qs(urlsplit(m.group(0)).query).get("referer", [""])[0]
        except ValueError:
            continue
        ref = unquote(ref)
        if is_tracker_url(ref):
            return ref
    return ""


def classify(data: dict) -> str | None:
    """'datadome', 'jigsaw', 'hcaptcha' or None for a navigate/snapshot result dict."""
    snap = str(data.get("snapshot") or "")
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    if (_DATADOME_URL.search(url) or _DATADOME_URL.search(_frames_text(data))
            or _DATADOME_TEXT.search(snap) or _DATADOME_TEXT.search(title)):
        return "datadome"
    if _JIGSAW_TEXT.search(snap) and re.search(r"\bCanvas\b", snap):
        return "jigsaw"
    if _HCAPTCHA_FRAME.search(snap) and _HCAPTCHA_CONTROLS.search(snap):
        return "hcaptcha"
    return None


def _end_now() -> str:
    return (
        "End NOW with:\n"
        "STATUS: captcha_failed\n"
        f"DETAIL: {CAPTCHA_FAILED_DETAIL}\n"
        "FIELDS_FILLED_JSON: {}"
    )


def datadome_notice(tracker: str) -> str:
    head = (
        "[jobo: this page is a DataDome bot wall (\"Verification Required\" / "
        "\"Slide right to secure your access\"). It CANNOT be passed: a real slide and "
        "the audio digits were both rejected in testing, because it judges the browser, "
        "not the answer. Do not drag, do not try the audio, do not take screenshots. "
    )
    if tracker and _state["tracker_visits"] < MAX_TRACKER_VISITS and _state["walls"] <= 1:
        return head + (
            f"It came from a job-board click tracker ({_host(tracker)}), which only "
            "redirects to the employer's job page. Do ONE thing: browser_navigate to this "
            f"exact URL once more: {tracker}\n"
            "It usually redirects to the employer's careers page on the second visit. "
            "There, use the employer's own Apply / Apply Now link and never go back to "
            "the tracker. If the wall shows again, " + _end_now() + "]"
        )
    return head + (
        "If the employer's own careers page is already open in another tab, switch to "
        "it and use its own Apply link. Otherwise " + _end_now() + "]"
    )


def hcaptcha_notice(host: str) -> str:
    head = (
        "[jobo: an hCaptcha image challenge is on screen. The agent cannot solve these "
        "(never once, across Oracle, iCIMS and Lever), so do not look at it, click tiles, "
        "drag, skip or refresh. "
    )
    if is_gate_host(host):
        return head + (
            f"{host} puts it in front of the whole application for every visitor (tested "
            "in a fresh browser), so this job is parked and costs no attempt. End NOW with:\n"
            "STATUS: captcha_wall\n"
            f"DETAIL: hCaptcha challenge on the email/sign-in step at {host}\n"
            "FIELDS_FILLED_JSON: {}]"
        )
    return head + (
        "End NOW with:\n"
        "STATUS: captcha_failed\n"
        f"DETAIL: hCaptcha image challenge at {host or 'this form'}; cannot be solved by the agent\n"
        "FIELDS_FILLED_JSON: {}]"
    )


JIGSAW_NOTICE = (
    "[jobo: this form has a jigsaw 'Slide to verify' captcha. Do not drag it yourself "
    "and do not study it with browser_vision. Fill every other field first, then print "
    "NEED_SLIDER on its own line and stop; the pipeline solves it in code and tells you "
    "when it is verified.]"
)


def observe(tool_name: str, result) -> tuple[str, str] | None:
    """(kind, notice) for a navigate/snapshot result showing a wall, else None.

    Must see the UNFILTERED result: the snapshot filter drops the wall's text
    ("Slide right to secure your access", the Canvas rows).
    """
    if tool_name not in ("browser_navigate", "browser_snapshot") or not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    _state["url"] = _page_url(data) or _state["url"]
    kind = classify(data)
    was_wall = _state["datadome"]
    _state["datadome"] = kind == "datadome"
    _state["hcaptcha"] = kind == "hcaptcha"
    if not kind:
        return None
    if kind == "hcaptcha":
        return kind, hcaptcha_notice(_host(_state["url"]))
    if kind == "datadome":
        if not was_wall:
            _state["walls"] += 1
        return kind, datadome_notice(wall_tracker(data))
    return kind, JIGSAW_NOTICE


def prepend(result: str, notice: str, kind: str = "") -> str | None:
    """Put `notice` at the top of a result's snapshot. Call AFTER filtering:
    snapshot_filter keeps only '- role' lines and would drop a notice."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["snapshot"] = notice + "\n" + str(data.get("snapshot") or "")
    if kind:
        data["jobo_captcha_wall"] = kind
    return json.dumps(data, ensure_ascii=False)


def annotate(tool_name: str, result):
    """observe + prepend in one go (for callers that do no filtering)."""
    seen = observe(tool_name, result)
    return prepend(result, seen[1], seen[0]) if seen else None
