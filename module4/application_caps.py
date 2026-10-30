"""A site's cap on applications per candidate: wait it out, charge nothing (GHO-110).

Measured 2026-10-30 (GHO-110): amazon.jobs answered job 2371 with "You've
reached the maximum number of job applications." (the page URL ends in
?result=application_limit_reach) minutes after the 22:00 run had sent it three
applications and the 14:00 run one. Amazon does not publish the rule, and
jobo's own history rules out the simple ones: it is not a daily count (24 Oct
took eight) and not a short rolling one (19 went through in the seven days to
24 Oct); 28 had gone through in the twelve days before the refusal. So the
window is unknown. The host is parked for its cool-down and the first job
there afterwards finds out; a second refusal parks it again, still uncharged.

How a job gets here:
  * the agent's DETAIL quotes the refusal (cap_host); the runner parks the
    job on the host it was applying on (the routed form URL, else the host
    the agent names) until the cool-down ends, and charges no attempt;
  * before an agent, a job whose form URL is on a host with a running
    cool-down is parked the same way;
  * the LinkedIn router parks a listing whose Apply link lands on such a host.

A parked job keeps should_apply and its attempts, and is queued again with no
DB change once the end time in its status has passed. `--job-id` / `--force`
still reach it, for testing.

LinkedIn's own Easy Apply limit is not handled here: linkedin.com hosts every
listing, so parking it would park the whole queue.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from module4.captcha_walls import own_host

STATUS_PREFIX = "app_cap:"

# Days a host stays parked after it refuses. Amazon's window is unknown (see
# above); a week keeps the retry cost to one short agent run per week.
COOLDOWN_DAYS: dict[str, int] = {"amazon.jobs": 7}
DEFAULT_COOLDOWN_DAYS = 1

_CAP_MESSAGE = re.compile(
    r"maximum number of (?:job )?applications|application[_ ]limit[_ ]reach|"
    r"(?:reached|hit|exceeded) (?:the |your |an? )?(?:daily |weekly |monthly )?"
    r"(?:application|applications|apply) (?:limit|cap)\b",
    re.I,
)
_HOST_NAME = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
_STATUS = re.compile(r"^app_cap:(\S+) until (\d{4}-\d\d-\d\dT\d\d:\d\d)Z")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _site(host: str) -> str:
    h = (host or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def cap_host(detail: str | None, url: str | None) -> str | None:
    """The host to park when `detail` reports a per-candidate application cap.

    The form's own URL when it is known; else (a LinkedIn listing the agent
    followed out) the first other host the agent names.
    """
    if not _CAP_MESSAGE.search(detail or ""):
        return None
    named = (own_host(f"https://{h}") for h in _HOST_NAME.findall(detail or ""))
    return own_host(url) or next((h for h in named if h), None)


def cooldown_days(host: str) -> int:
    site = _site(host)
    return next((d for s, d in COOLDOWN_DAYS.items() if site == s or site.endswith("." + s)),
                DEFAULT_COOLDOWN_DAYS)


def cooldown_end(host: str, now: datetime | None = None) -> datetime:
    return _now(now) + timedelta(days=cooldown_days(host))


def status_for(host: str, until: datetime, detail: str) -> str:
    """module4_last_status for a parked job: 'app_cap:<host> until <UTC> | <detail>'."""
    return f"{STATUS_PREFIX}{host} until {until.astimezone(timezone.utc):%Y-%m-%dT%H:%M}Z | {detail}"[:600]


def parked_until(last_status: str | None) -> tuple[str, datetime] | None:
    m = _STATUS.match((last_status or "").strip())
    if not m:
        return None
    until = datetime.strptime(m.group(2), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return m.group(1), until


def is_parked(last_status: str | None, now: datetime | None = None) -> bool:
    """True while the cool-down in this status is still running."""
    p = parked_until(last_status)
    return bool(p and p[1] > _now(now))


def active_caps(rows, now: datetime | None = None) -> dict[str, tuple[str, datetime]]:
    """site -> (host, end) for every cool-down still running among `rows`."""
    caps: dict[str, tuple[str, datetime]] = {}
    for r in rows:
        p = parked_until(getattr(r, "module4_last_status", None))
        if p and p[1] > _now(now):
            site = _site(p[0])
            if site not in caps or p[1] > caps[site][1]:
                caps[site] = p
    return caps


def capped_host_for_url(url: str | None, caps: dict[str, tuple[str, datetime]]) -> tuple[str, datetime] | None:
    """(host, end) of the running cool-down `url`'s host is under, or None."""
    site = _site(own_host(url))
    return caps.get(site) if site else None


def pre_park_detail(host: str, until: datetime) -> str:
    return (f"{host} refused another job with its application cap; waiting until "
            f"{until.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC before applying there again")
