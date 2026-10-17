"""ATS sites the apply agent must not work on for now.

Unlike policy.employer_blocklist (a standing "never apply there"), a pause is
temporary: the site's forms cannot currently be completed by the agent, so
every attempt burns budget and submits nothing. Jobs that land on a paused
site keep their attempts and come back into the queue as soon as the site is
removed from this list.

Why Rippling (2026-09-28): in the 100 apply sessions since 2026-09-11, four
Rippling applications consumed ~121M tokens (53.5M, 33.8M, 22.9M, 10.9M -
roughly 30% of all apply tokens in that window) and none was submitted. Its
custom "Select" comboboxes discard both clicked and programmatic selections.

The destination is only known once the agent follows "Apply on company
website", so the agent reports STATUS: paused_site (see module4/prompts.py);
a job_url that is already on a paused site is skipped before any agent runs.

`JOBO_PAUSED_SITES` (comma separated domains) adds entries; like the employer
blocklist, env input can only add to the built-in list.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

PAUSED_SITES: tuple[str, ...] = ("ats.rippling.com",)

STATUS_PREFIX = "paused:"


def paused_sites() -> tuple[str, ...]:
    extra = [
        s.strip().lower()
        for s in os.environ.get("JOBO_PAUSED_SITES", "").split(",")
        if s.strip()
    ]
    return tuple(dict.fromkeys([*PAUSED_SITES, *extra]))


def _matches(host: str, site: str) -> bool:
    host = host.lower().rstrip(".")
    return host == site or host.endswith("." + site)


def paused_site_for_url(url: str) -> str | None:
    """The paused site this URL belongs to, or None."""
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return None
    return next((s for s in paused_sites() if host and _matches(host, s)), None)


def paused_site_in_text(text: str) -> str | None:
    """The paused site named in free text (an agent DETAIL line), or None."""
    t = (text or "").lower()
    return next((s for s in paused_sites() if s in t), None)


def status_for(site: str, detail: str) -> str:
    """module4_last_status value recorded for a job parked on a paused site."""
    return f"{STATUS_PREFIX}{site} | {detail}"[:600]


def still_paused(last_status: str | None) -> bool:
    """True while the site a job was parked on is still in the paused list.

    Removing a site from PAUSED_SITES releases its jobs with no DB change.
    """
    s = (last_status or "").strip()
    if not s.startswith(STATUS_PREFIX):
        return False
    site = s[len(STATUS_PREFIX):].split("|", 1)[0].strip().lower()
    return site in paused_sites()


def prompt_text() -> str:
    return ", ".join(f"**{s}**" for s in paused_sites())
