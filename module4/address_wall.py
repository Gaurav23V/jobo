"""Forms that require a street address or postal code: park the job, charge nothing.

G's candidate facts hold no street address or PIN/ZIP code (asked on GHO-93,
25 Oct). A form that will not go on without one stops every attempt at the
same field, and the agent rightly refuses to invent it. The 2026-10-30 22:00
run had 6 of its 13 stuck jobs stop there (Greenhouse, LinkedIn Easy Apply,
Workday, Tenstreet, Oracle CX, Paradox; GHO-109). They were filed three ways:
the agent's wording decided it. "requires X ... not in the candidate facts"
matched blockers._MISSING_FACT and was dropped for good; "requires X and Y,
neither of which is in the candidate facts" matched nothing and was retried,
each retry a full agent run to reach the same field.

Now every such stop is parked under one marker:

  * module4_last_status becomes 'needs_address: <detail>' and no attempt is
    charged; should_apply stays 1, so the job is still in the funnel;
  * the apply queue skips it without an agent (`--force` still reaches it);
  * `jobo run-report` files it as personal:address (needs_g);
  * once G's answer is in the candidate facts,
    `scripts/requeue_address_parked.py --apply` puts every parked job back in
    the queue with its attempts reset.

A form that also wants a photo, a date of birth or an ID number is not parked
here: those walls stay where module4/blockers.py puts them (blockers.address_park).
"""

from __future__ import annotations

import re

STATUS_PREFIX = "needs_address:"

# The fields. "email address" and "IP address" are not these.
_FIELD = re.compile(
    r"\b(?:(?:street|home|residential|mailing|postal|physical|permanent|current) address|"
    r"address line|address ?1|zip(?:/postal)?(?: code)?|postal code|post ?code|pin ?code|pincode)\b",
    re.I,
)
# The agent saying the form will not go on without it.
_DEMANDS = re.compile(r"requir|mandat|compulsory|blocked", re.I)
# ... and that the value is not in the candidate facts, in its many wordings:
# "not in the candidate facts", "neither of which is in the candidate facts",
# "none of which are present in candidate facts".
_NOT_IN_FACTS = re.compile(
    r"\b(?:not|neither|none)\b[^.;]{0,60}?\bin (?:the )?candidate(?:'s)? facts|"
    r"candidate facts (?:do(?:es)? not|don'?t|lack|have no|contain no|omit)",
    re.I,
)


def is_address_wall(detail: str | None) -> bool:
    """The form requires a street address or postal code the candidate facts lack."""
    d = detail or ""
    return bool(_FIELD.search(d) and _DEMANDS.search(d) and _NOT_IN_FACTS.search(d))


def status_for(detail: str) -> str:
    """module4_last_status for a parked job: 'needs_address: <detail>'."""
    return f"{STATUS_PREFIX} {detail}"[:600]


def is_parked(last_status: str | None) -> bool:
    return (last_status or "").strip().startswith(STATUS_PREFIX)
