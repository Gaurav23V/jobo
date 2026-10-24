"""ATS hosts that only take applications behind an outside identity: drop before any agent.

Measured 2026-10-24 (GHO-92): jobs.apple.com shows no application form until
the visitor signs in with an Apple Account. Signing in to the existing one
needs approval on a trusted device or SMS (611, Sept), the reset page has a
captcha the agent never passed (1854, 17 Oct), and creating a new one runs in
nested cross-origin iframes with closed shadow DOM: 2095 spent all 80 model
steps there and typed a made-up date of birth into Apple's signup. Every
Apple job so far was dropped with the external-identity reason
(module4/blockers.py), and that needs G (an Apple ID that G approves), not
another attempt.

So a job whose job_url, or whose LinkedIn Apply link, is on one of these
hosts is dropped with that same reason before any agent runs. `--force`
still reaches it, for testing.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Host suffix -> what the site makes the visitor sign in with. The text goes
# into module4_last_status, where blockers.permanent_reason and run_report
# read it ("apple id" / "apple account").
IDENTITY_WALLS: dict[str, str] = {
    "jobs.apple.com": "an Apple Account (Apple ID approved on a trusted device or SMS)",
}


def identity_wall_for_url(url: str | None) -> str | None:
    """The identity-wall host `url` is on, or None."""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return next((s for s in IDENTITY_WALLS if host and (host == s or host.endswith("." + s))), None)


def status_for(host: str) -> str:
    """module4_last_status for a job dropped on arrival at an identity wall."""
    return f"stuck:{host} only accepts applications after sign-in with {IDENTITY_WALLS[host]}; not attempted"


def prompt_text() -> str:
    return ", ".join(f"**{h}**" for h in IDENTITY_WALLS)
