"""Ranked fallback answers for form fields whose site list has no exact match.

Why this exists
---------------
On the 2026-09-06 run the single most expensive pattern was an agent hunting
for a value the site's own list does not contain, one guess per turn:

    NEED_FIELD: #19 = Bachelor's Degree     -> no option matched
    NEED_FIELD: #19 = Bachelor - Engineering
    NEED_FIELD: #35 = Engineering           -> no option matched
    NEED_FIELD: #35 = Information Technology

job516 burned its budget cycling three spellings of the candidate's college
through a Greenhouse School field that has none of them. Each guess costs a full agent
turn plus a ~600ms settle, and the agent has no way to see the list it is
guessing against until it fails.

The fix is to stop guessing in the agent and start choosing in code: the
driver already knows the options the widget is offering, so it can walk a
ranked list of things the candidate could honestly answer and pick the first one the site
actually offers.

Rules
-----
- Every fallback must remain TRUE for the candidate. The school's full name
  is the same school spelled out; a similarly named different institution is
  not, and must never be listed. Never add a fallback that makes a claim the
  candidate cannot back.
- The school and city chains are built from personal/candidate.json
  (`school_aliases`, `city_aliases`; see policy/personal.py), so nothing about
  the candidate is hard-coded here.
- Generic last-resort entries ("Other", "Prefer not to say") come last and are
  only ever reached when nothing specific matched.
- Matching is done against the options the SITE offers, so a fallback that no
  form lists simply never fires.
"""

from __future__ import annotations

import re
from typing import Optional

from policy import personal

# Escapes that school catalogues offer when the school is missing (Wingtip's
# Avature School search had none of the candidate's spellings), and the form's
# own help text says to pick one.
_SCHOOL_ESCAPES = (
    "School not listed",
    "College not listed",
    "University not listed",
    "Institution not listed",
    # Workday's School catalogue escape (J&J 1885, 2026-10-18); the form's
    # help text says to pick it when the school is missing.
    "Other - See Resume",
    "Other",
    "Other (not listed)",
    "Not listed",
)

# Ordered most-specific first. The first entry that the site's option list
# actually contains wins.
_CHAINS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    # --- College / school: built from personal config, see _personal_chains()
    # --- Degree -----------------------------------------------------------
    (
        ("bachelor", "bachelors", "b tech", "btech", "b.tech", "bachelor's degree"),
        (
            "Bachelor's Degree",
            "Bachelors Degree",
            "Bachelor - Engineering",
            "Bachelor of Technology",
            "Bachelor of Engineering",
            "Bachelor",
            "Undergraduate",
            "Bachelor's",
            "BE/B.Tech",
            "Graduate",
        ),
    ),
    # --- Field of study / discipline --------------------------------------
    (
        ("computer science", "cs", "comp sci", "computer science and engineering"),
        (
            "Computer Science",
            "Computer Science and Engineering",
            "Computer Science & Engineering",
            "Computer Engineering",
            "Information Technology",
            "Engineering",
            "Computer and Information Science",
            "Other",
        ),
    ),
    # --- Industry ---------------------------------------------------------
    (
        ("technology", "tech", "software", "it"),
        (
            "Technology",
            "Information Technology",
            "Software",
            "Computer Software",
            "Internet",
            "Engineering",
            "Other",
        ),
    ),
    # --- Job function -----------------------------------------------------
    (
        ("engineering", "software engineering", "information technology"),
        (
            "Engineering",
            "Information Technology",
            "Software Engineering",
            "Technology",
            "Engineering - Software",
            "IT",
            "Other",
        ),
    ),
    # --- Country ----------------------------------------------------------
    (
        ("india", "in"),
        ("India", "IN", "India (IN)", "IND"),
    ),
    # --- City: built from personal config, see _personal_chains()
    # --- Yes/no phrasings -------------------------------------------------
    (
        ("yes",),
        ("Yes", "Y", "True", "I agree", "Agree", "I confirm", "Confirmed"),
    ),
    (
        ("no",),
        ("No", "N", "False", "I disagree", "Disagree"),
    ),
]


def _personal_chains() -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """School and city chains from personal/candidate.json, most specific first."""
    chains: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for key, escapes in (("school", _SCHOOL_ESCAPES), ("city", ())):
        triggers, options = personal.answer_chain(key)
        if key == "school":
            # Every school spelling is the same school, so asking for any of
            # them (on J&J 1885 the agent asked for the bare acronym) walks the
            # chain. Not cities: a state name in that chain is not the city.
            triggers = list(triggers) + list(options)
        if triggers and options:
            chains.append((tuple(triggers), tuple(options) + escapes))
    return chains


def _all_chains() -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    return _personal_chains() + _CHAINS


def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_INSTITUTION_WORDS = frozenset(
    ("institute", "institution", "university", "college", "school", "academy", "polytechnic")
)


def _is_institution(value: str) -> bool:
    """True for a school name: an institution word, or one of the candidate's
    own school spellings (which include bare acronyms)."""
    n = _norm(value)
    if not n:
        return False
    if _INSTITUTION_WORDS & set(n.split()):
        return True
    triggers, options = personal.answer_chain("school")
    return n in {_norm(s) for s in list(triggers) + list(options)}


# Search-as-you-type widgets (Workday School) only show what the query
# matches, so every escape phrasing would cost its own search. These two
# queries surface all of _SCHOOL_ESCAPES ("Other - See Resume", "School not
# listed", ...); pick_from_options then chooses among the results.
_ESCAPE_QUERIES = ("Other", "not listed")


def search_queries_for(value: str) -> list[str]:
    """Queries worth typing into a search widget that found nothing for `value`.

    The specific fallbacks in order, then one query per escape family when the
    chain has escapes. Generic entries never come before specific ones.
    """
    specific: list[str] = []
    has_escape = False
    for alt in fallbacks_for(value):
        n = _norm(alt)
        if n.startswith("other") or "not listed" in n:
            has_escape = True
        elif n not in {_norm(s) for s in specific}:
            specific.append(alt)
    return specific + (list(_ESCAPE_QUERIES) if has_escape else [])


def fallbacks_for(value: str) -> list[str]:
    """Ranked alternative phrasings for `value`, most faithful first.

    Returns [] when we have nothing honest to offer, which is the common case
    for free-text answers and must stay cheap.
    """
    want = _norm(value)
    if not want:
        return []
    chains = _all_chains()
    for triggers, chain in chains:
        if any(want == _norm(t) for t in triggers):
            return [c for c in chain if _norm(c) != want]
    # Substring trigger, so "Bachelor of Technology in Computer Science" still
    # reaches the degree chain. Longest trigger wins to avoid "cs" hijacking a
    # sentence that merely contains it.
    best: Optional[tuple[int, tuple[str, ...]]] = None
    for triggers, chain in chains:
        for t in triggers:
            tn = _norm(t)
            if len(tn) < 4:
                continue  # too short to match on substring safely
            if tn in want and (best is None or len(tn) > best[0]):
                best = (len(tn), chain)
    if best:
        return [c for c in best[1] if _norm(c) != want]
    return []


def pick_from_options(value: str, options: list[str]) -> Optional[str]:
    """Choose the option the SITE offers that best represents `value`.

    `options` is the live option list read out of the widget. Returns the exact
    option text to select, or None when the site genuinely offers nothing that
    is true for the candidate - which is a real blocker the agent must be told about
    honestly rather than papered over with an invented option.
    """
    if not options:
        return None
    norm_opts = [(o, _norm(o)) for o in options if (o or "").strip()]
    want = _norm(value)
    if not want:
        return None

    # 1. Exact.
    for orig, n in norm_opts:
        if n == want:
            return orig
    # 2. Ranked honest alternatives.
    for alt in fallbacks_for(value):
        alt_n = _norm(alt)
        for orig, n in norm_opts:
            if n == alt_n:
                return orig
    # 3. Containment, longest option first so "Computer Science and
    #    Engineering" beats a bare "Engineering" when both would match.
    #    Matching is TOKEN-level, never raw substring: an abbreviation like
    #    "abcd tech" contains the substring "bcd tech", so a substring rule
    #    would cheerfully answer a different institution's name - a lie on a
    #    real application. Requiring whole tokens makes that impossible while
    #    still letting "Computer Science" match "Computer Science and
    #    Engineering".
    #    Never for a school, though: whole tokens still let the candidate's
    #    bare acronym match "<acronym> <other city>", a different institution
    #    (J&J 1885, 2026-10-18). A school matches only through its own
    #    spellings above or an escape below.
    want_toks = want.split()
    if len(want) >= 4 and want_toks and not _is_institution(value):
        for orig, n in sorted(norm_opts, key=lambda p: -len(p[1])):
            if len(n) < 4 or _is_institution(orig):
                continue
            opt_toks = n.split()
            if not opt_toks:
                continue
            if all(t in opt_toks for t in want_toks) or all(
                t in want_toks for t in opt_toks
            ):
                return orig
    # 4. A generic escape hatch, only if the form offers one.
    for generic in ("Other", "Other (please specify)", "Not listed", "Prefer not to say"):
        gn = _norm(generic)
        for orig, n in norm_opts:
            if n == gn:
                return orig
    return None
