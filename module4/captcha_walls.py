"""Captcha walls on an ATS's email / sign-in gate or whole site: park the job, charge nothing.

Measured 2026-10-18 (GHO-54). Oracle Cloud Recruiting (*.oraclecloud.com,
Candidate Experience) and iCIMS (*.icims.com) put an INVISIBLE hCaptcha on the
"enter your email" step, the gate in front of the whole application. In a
fresh scratch Chromium (new profile, headed, not the jobo profile, never used
for automation, email typed at human speed, a real mouse click on Next) both
showed the full image challenge (a 520x570 frame) on the first Next. So it is
not our profile, session or speed: every visitor without an hCaptcha
reputation gets the puzzle. The agent has never solved one (Oracle: 4 jobs, 4
walls, 0 submissions ever), and each attempt spent 15-17 model steps finding
that out.

Measured 2026-10-22 (GHO-73): Paycom (www.paycomonline.net) and Lever
(jobs.lever.co, jobs.eu.lever.co) are SITE walls. Executing their own hCaptcha
sitekey in two fresh scratch profiles gave an image or drag puzzle at once on
every page tried (2 Paycom tenants, 3 Lever postings, US and EU). Lever used to
pass some visitors (750 and 920 went through on a retry in September), but it
now loads hCaptcha's enclave mode (secure-api.js): a page-sized iframe that
covers Submit, with the challenge nested inside it. Since 4 Oct every Lever
attempt ended captcha_failed (1496, 1662, 1739, 2134; 2134 spent 72 steps
clicking Submit). Paycom's challenge frame is hidden until shown and never
reaches the accessibility snapshot (2197 spent 33 steps). The plugin cannot
see either challenge, so on these hosts the job is parked on arrival.

Measured 2026-10-23 (GHO-81): Cutshort (cutshort.io) is a Cloudflare Turnstile
wall. After the resume reaches Cutshort's upload bucket, its save-resume step
runs turnstile.reset() + execute(); in the jobo browser that widget hangs with
error 300030 every ~45 s and POST /save-resume-in-user returns 401, so the
form sits at "Reading your resume..." forever. A live retry of 1858 showed it
in the production profile with the agent already gone, and the 22:00 run hit
it again. Nine Cutshort jobs, none ever submitted. (A clean scratch Chromium
got Turnstile tokens on the same sitekey, so this may be our browser's
reputation rather than every visitor; either way the agent cannot pass it.)

A challenge on any other host stays an ordinary captcha_failed.

How a job gets here:
  * in a run, hermes_plugin/jobo-browser/captcha_guard.py sees the challenge on
    a gate host (or the agent reaches a site-wall host) and tells the agent to
    end with STATUS: captcha_wall;
  * before a run, a job whose job_url is on a site-wall host, or on a host that
    already parked another job, is parked without an agent
    (walled_host_for_url); so is a LinkedIn job the parallel router finds
    applying on a site-wall host.

A parked job keeps should_apply and its attempts; runs skip it. It needs a
policy call from G to go further (no captcha-solving service without
asking G). `--job-id` / `--force` still reach it, for testing.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Keep these in sync with hermes_plugin/jobo-browser/captcha_guard.py (the
# plugin cannot import module4); scripts/test_hcaptcha_walls.py checks it.
# Site-wall host suffix -> the captcha it shows.
SITE_WALLS: dict[str, str] = {
    "paycomonline.net": "hCaptcha",
    "lever.co": "hCaptcha",
    "cutshort.io": "Cloudflare Turnstile",
}
SITE_WALL_HOST_SUFFIXES: tuple[str, ...] = tuple(SITE_WALLS)
GATE_HOST_SUFFIXES: tuple[str, ...] = ("oraclecloud.com", "icims.com") + SITE_WALL_HOST_SUFFIXES

STATUS_PREFIX = "captcha_wall:"

_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)


def host_of(url: str | None) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _matches(host: str | None, suffixes: tuple[str, ...]) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in suffixes)


def is_gate_host(host: str | None) -> bool:
    return _matches(host, GATE_HOST_SUFFIXES)


def is_site_wall_host(host: str | None) -> bool:
    return _matches(host, SITE_WALL_HOST_SUFFIXES)


def wall_captcha(host: str | None) -> str:
    """The captcha a site-wall host shows ('hCaptcha' for the gate hosts)."""
    h = (host or "").lower().rstrip(".")
    return next((c for s, c in SITE_WALLS.items() if h == s or h.endswith("." + s)), "hCaptcha")


def site_wall_host_for_url(url: str | None) -> str | None:
    """The host of `url` if it is a site wall (any url, a routed apply link too)."""
    h = host_of(url)
    return h if is_site_wall_host(h) else None


def gate_host_in_text(text: str | None) -> str | None:
    """The first gate-family host named in free text (an agent DETAIL), or None."""
    return next((h.lower() for h in _HOST.findall(text or "") if is_gate_host(h)), None)


def status_for(host: str, detail: str) -> str:
    """module4_last_status for a parked job: 'captcha_wall:<host> | <detail>'."""
    return f"{STATUS_PREFIX}{host} | {detail}"[:600]


def is_parked(last_status: str | None) -> bool:
    s = (last_status or "").strip()
    return s.startswith(STATUS_PREFIX) and is_gate_host(s[len(STATUS_PREFIX):].split("|", 1)[0].strip())


def own_host(url: str | None) -> str:
    """job_url host, or '' for LinkedIn (its jobs go to many different ATSes)."""
    h = host_of(url)
    return "" if not h or h == "linkedin.com" or h.endswith(".linkedin.com") else h


def walled_job_hosts(rows) -> set[str]:
    """job_url hosts of parked jobs. Another job on one of them meets the same wall."""
    return {
        h for r in rows
        if is_parked(getattr(r, "module4_last_status", None))
        and (h := own_host(getattr(r, "job_url", None)))
    }


def walled_host_for_url(url: str | None, walled: set[str]) -> str | None:
    h = own_host(url)
    return h if h and (h in walled or is_site_wall_host(h)) else None


def pre_park_detail(host: str) -> str:
    """Why a job on `host` was parked before any agent ran."""
    if wall_captcha(host) == "Cloudflare Turnstile":
        return (f"Cloudflare Turnstile on {host}'s resume step never passes in the jobo browser "
                "(error 300030, save-resume 401), so the application is never saved")
    if is_site_wall_host(host):
        return f"hCaptcha challenges every visitor at {host} before the application goes through"
    return "another job on this host is parked behind its hCaptcha sign-in wall"
