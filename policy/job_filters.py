"""Rule filters that drop unwanted jobs before they cost a triage call.

One module, used in two places (see docs/sources.md):

  * at collection time, for sources with structured facts (jobfound): a job
    that fails is never stored;
  * after enrich and before triage, for every source (``jobo prefilter``): a
    row that fails is marked ``prefilter_reason`` and module 3 skips it.

Every threshold and list is a named constant at the top of this file, so the
rules can be tuned without reading the logic below.

Rules run only when their inputs are known. Title rules (intern, QA) work for
every row. Pay, experience and category rules run only when the value is
structured (a jobfound tag) or parses with high confidence; anything unclear
lets the job through, because a wrongly dropped job costs more than one
triage call. There is deliberately NO location rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from module3.exclusions import _INTERN_RE

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Drop reasons (stable keys: they are stored on rows and counted in reports).
REASON_INTERNSHIP = "internship"
REASON_QA = "qa_testing"
REASON_NON_TECH = "non_tech"
REASON_TOO_SENIOR = "too_senior"
REASON_LOW_PAY = "low_pay"

# Pay: an LPA salary whose top of range is BELOW this is dropped. "10-12 LPA"
# has top 12 and is kept; "8-11 LPA" is dropped. Non-LPA pay ($, GBP, "Competitive")
# is never dropped here; triage's pay rule handles it.
MIN_TOP_LPA = 12.0

# Experience: dropped when the MINIMUM years asked for is at least this.
# "5++ years" -> 5 -> dropped; "2-4+ years" -> 2 -> kept.
MAX_MIN_YEARS = 5.0

# Job types (jobfound "Job Type:" tag, or a structured employment_type) that
# mean internship. Compared case-insensitively.
INTERNSHIP_JOB_TYPES = frozenset({"internship", "intern"})
INTERNSHIP_CATEGORIES = frozenset({"internship"})

# Categories that count as a tech role. A job is kept by the category rule if
# ANY of its categories is here. Compared case-insensitively after unescaping.
TECH_CATEGORIES = frozenset(
    {
        "engineering and development",
        "software engineer",
        "software engineering",
        "software development",
        "application development",
        "frontend development",
        "backend development",
        "full stack development",
        "mobile development",
        "web development",
        "data & ai",
        "data and ai",
        "data science",
        "data engineering",
        "data analytics",
        "data analysis",
        "business intelligence",
        "machine learning",
        "artificial intelligence",
        "devops",
        "cloud",
        "cloud computing",
        "site reliability engineering",
        "cybersecurity",
        "information security",
    }
)

# Categories that say nothing about tech vs non-tech; ignored by the category
# rule (Internship and Quality Assurance have their own rules). A job whose
# only categories are neutral is decided by its title (TECH_TITLE_RE).
NEUTRAL_CATEGORIES = frozenset({"entry level", "internship", "quality assurance"})

QA_CATEGORIES = frozenset({"quality assurance"})

# QA/testing titles. Whole words; "Backend Engineer, Testing Infrastructure"
# does NOT match (no pattern below covers "testing infrastructure").
QA_TITLE_RE = re.compile(
    r"\bQA\b"
    r"|\bquality\s+assurance\b"
    r"|\btest(?:ing)?\s+engineer"
    r"|\bSDET\b"
    r"|\btesters?\b"
    r"|\b(?:manual|automation)\s+test(?:ing|er|ers)?\b"
    r"|\btest\s+automation\b"
    r"|\bsoftware\s+(?:development\s+)?engineer\s+in\s+test\b",
    re.I,
)

# Titles that look like a tech role; used only when a job's categories are all
# neutral (e.g. only "Entry Level").
TECH_TITLE_RE = re.compile(
    r"\b(?:engineer|engineering|developer|programmer|software|sde|swe|devops|sre"
    r"|full[\s-]?stack|front[\s-]?end|back[\s-]?end|data\s+(?:scientist|analyst|engineer)"
    r"|machine\s+learning|ml|ai|llm|cloud|platform|infrastructure|python|java|golang"
    r"|react|node(?:\.?js)?|android|ios|web)\b",
    re.I,
)

# Value parsers. Only full matches count as "known"; anything else is unknown
# and the rule is skipped.
_LPA_RE = re.compile(
    r"^\s*(?:₹|inr|rs\.?)?\s*(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(?:₹|inr|rs\.?)?\s*(\d+(?:\.\d+)?)\s*lpa\s*$"
    r"|^\s*(?:₹|inr|rs\.?)?\s*(\d+(?:\.\d+)?)\s*lpa\s*$",
    re.I,
)
_YEARS_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*\+*\s*(?:years?|yrs?)\s*\+*\s*$",
    re.I,
)

# Description length (characters of text) under which a description is too
# thin to judge fit; sources with a richer page fetch it (jobfound JSON-LD).
THIN_DESCRIPTION_CHARS = 300


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass
class JobFacts:
    """What the rules look at. None/empty = unknown, so that rule is skipped."""

    title: Optional[str] = None
    job_type: Optional[str] = None
    categories: list[str] = field(default_factory=list)
    salary: Optional[str] = None
    experience: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type,
            "categories": list(self.categories),
            "salary": self.salary,
            "experience": self.experience,
        }


def _unescape(s: str) -> str:
    import html

    return html.unescape(s or "").strip()


def facts_from_labels(title: Optional[str], labels: Iterable[str]) -> JobFacts:
    """Facts from jobfound category labels.

    ``Job Type: X``, ``Salary: X``, ``Experience: X`` become structured facts;
    ``Location: X`` is ignored (no location rule); any other label is a category.
    """
    facts = JobFacts(title=title)
    for raw in labels:
        label = _unescape(raw)
        if not label:
            continue
        key, sep, value = label.partition(":")
        k = key.strip().lower()
        v = value.strip()
        if sep and k == "job type":
            facts.job_type = v or None
        elif sep and k == "salary":
            facts.salary = v or None
        elif sep and k == "experience":
            facts.experience = v or None
        elif sep and k == "location":
            continue
        else:
            facts.categories.append(label)
    return facts


def _metadata_value(meta: dict[str, Any], *names: str) -> Any:
    lowered = {str(k).strip().lower().replace(" ", "_"): v for k, v in meta.items()}
    for n in names:
        if n in lowered:
            return lowered[n]
    return None


def facts_from_row(job_title: Optional[str], metadata_json: Optional[str]) -> JobFacts:
    """Facts for a stored row (any source).

    Structured facts come from ``metadata_json['_facts']`` (written by sources
    that have them, see sources/base.py). For rows without them (LinkedIn
    email rows enriched by module 2), the only extra fact used is an
    ``employment_type`` that is exactly an internship type: that is the one
    structured value Ollama copies verbatim from LinkedIn's job criteria.
    Free-text salary and experience are never parsed for these rows.
    """
    try:
        meta = json.loads(metadata_json or "{}")
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    raw = meta.get("_facts")
    if isinstance(raw, dict):
        cats = raw.get("categories") or []
        return JobFacts(
            title=job_title,
            job_type=raw.get("job_type") or None,
            categories=[str(c) for c in cats if c],
            salary=raw.get("salary") or None,
            experience=raw.get("experience") or None,
        )
    facts = JobFacts(title=job_title)
    et = _metadata_value(meta, "employment_type")
    if isinstance(et, str) and et.strip().lower() in INTERNSHIP_JOB_TYPES:
        facts.job_type = et.strip()
    return facts


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def lpa_top(salary: Optional[str]) -> Optional[float]:
    """Top of an LPA range ("4-6 LPA" -> 6.0), or None when not an LPA value."""
    m = _LPA_RE.match(salary or "")
    if not m:
        return None
    if m.group(2) is not None:
        return max(float(m.group(1)), float(m.group(2)))
    return float(m.group(3))


def min_years(experience: Optional[str]) -> Optional[float]:
    """Minimum years in an experience tag ("2-4+ years" -> 2, "5++ years" -> 5)."""
    m = _YEARS_RE.match(experience or "")
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _norm_cats(categories: Iterable[str]) -> set[str]:
    return {_unescape(c).lower() for c in categories if c and _unescape(c)}


def is_internship(f: JobFacts) -> bool:
    if f.job_type and f.job_type.strip().lower() in INTERNSHIP_JOB_TYPES:
        return True
    if _norm_cats(f.categories) & INTERNSHIP_CATEGORIES:
        return True
    return bool(_INTERN_RE.search(f.title or ""))


def is_qa(f: JobFacts) -> bool:
    if _norm_cats(f.categories) & QA_CATEGORIES:
        return True
    return bool(QA_TITLE_RE.search(f.title or ""))


def is_non_tech(f: JobFacts) -> bool:
    """True only when categories are known and none is tech.

    No categories at all (LinkedIn email rows) -> unknown -> kept.
    Only neutral categories ("Entry Level") -> decided by the title.
    """
    cats = _norm_cats(f.categories)
    if not cats:
        return False
    if cats & TECH_CATEGORIES:
        return False
    if cats <= NEUTRAL_CATEGORIES:
        return not TECH_TITLE_RE.search(f.title or "")
    return True


def is_too_senior(f: JobFacts) -> bool:
    years = min_years(f.experience)
    return years is not None and years >= MAX_MIN_YEARS


def is_low_pay(f: JobFacts) -> bool:
    top = lpa_top(f.salary)
    return top is not None and top < MIN_TOP_LPA


# Order matters only for which reason is reported first.
RULES = (
    (REASON_INTERNSHIP, is_internship),
    (REASON_QA, is_qa),
    (REASON_NON_TECH, is_non_tech),
    (REASON_TOO_SENIOR, is_too_senior),
    (REASON_LOW_PAY, is_low_pay),
)


def drop_reasons(f: JobFacts) -> list[str]:
    """Every rule the job fails, in RULES order. Empty list = keep."""
    return [name for name, rule in RULES if rule(f)]


def drop_reason(f: JobFacts) -> Optional[str]:
    """The first rule the job fails, or None to keep it."""
    reasons = drop_reasons(f)
    return reasons[0] if reasons else None
