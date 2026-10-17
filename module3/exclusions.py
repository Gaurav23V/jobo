"""Hard exclusions that hold whatever fit score the model returns.

Internships are out entirely (the candidate is past them). The profile tells the fit model the same thing, but a model can
still score a well-matched internship 4; this keeps it out of the apply
queue regardless. Only the title is checked: it is the one field every row
has, and an "Intern" title is unambiguous. A pay floor stays in the prompt
because posted salaries are free text in every shape and currency.
"""

from __future__ import annotations

import re

# Whole words only, so "Internal Tools Engineer" and "International
# Expansion" are not caught.
_INTERN_RE = re.compile(r"\bintern(?:s|ship|ships)?\b", re.I)


def exclusion_reason(job_title: str | None) -> str | None:
    """A short reason when the job must never be queued, else None."""
    if _INTERN_RE.search(job_title or ""):
        return "internship (excluded)"
    return None
