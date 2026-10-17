"""Where a queued job's job_url sends it in module 4. Pure: no browser, network or DB.

Most rows are LinkedIn listings (the alert emails, and ~84% of jobfound
rows). The rest of jobfound's rows link straight to the employer's ATS
(Greenhouse, Ashby, Workday, iCIMS, Rippling, careers pages). These
helpers name the difference once, for the runner, the parallel queue and
`jobo apply --plan`:

  * LinkedIn listing: the LinkedIn session check and the closed-listing
    preflight apply, and in a parallel run the LinkedIn worker opens it to
    route it (module4/linkedin_router.py).
  * direct employer link: no LinkedIn involved. In a parallel run it goes
    straight to a form worker, whose browser never signs in to LinkedIn.
  * direct Google Form: kept on the signed-in browser, the same rule the
    router uses for a Google Form behind a LinkedIn listing.
"""

from __future__ import annotations

from module4 import apply_queue, google_forms, linkedin_router

LINKEDIN = "linkedin"
GOOGLE_FORM = "google_form"
OTHER = "other"


def is_linkedin_listing(url: str | None) -> bool:
    return linkedin_router.is_linkedin_host(str(url or ""))


def ats_kind(url: str | None) -> str:
    """linkedin, google_form, an ATS family (greenhouse, ashby, workday, icims,
    rippling, lever, ...; module4/apply_queue.VENDOR_FAMILIES) or other."""
    u = str(url or "")
    if is_linkedin_listing(u):
        return LINKEDIN
    if google_forms.find_form_url(u):
        return GOOGLE_FORM
    family = apply_queue.family_for_url(u)
    return family if family in apply_queue.VENDOR_FAMILIES else OTHER


def initial_route(url: str | None) -> tuple[str | None, str | None, str]:
    """(route, apply_url, note) for a parallel run's queue before any routing.

    route None = unrouted: the LinkedIn worker claims it and routes it
    (linkedin_router.resolve), exactly as for every job before jobfound.
    """
    u = str(url or "")
    if is_linkedin_listing(u):
        return None, None, "LinkedIn listing: routed by the LinkedIn worker"
    if not linkedin_router._usable_external(u):
        # resolve() calls this UNKNOWN; the LinkedIn worker applies to it.
        return None, None, "not a usable URL: left to the LinkedIn worker"
    if google_forms.find_form_url(u):
        return apply_queue.ROUTE_LINKEDIN, u, "direct Google Form: the signed-in browser applies"
    return apply_queue.ROUTE_FORM, u, "direct employer link: a form worker applies"
