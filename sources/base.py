"""The normalized job candidate every source yields.

A source turns whatever it reads (an alert email, a feed entry, a job page)
into ``JobCandidate`` objects and hands them to
``sources.store.insert_candidates``. Nothing else about a source is shared.

Two kinds of candidate:

  * bare link (the LinkedIn alert email): only ``job_url`` is known. The row is
    inserted un-enriched and module 2 (browser + Ollama) enriches it later.
  * pre-enriched (jobfound): the source already has title, company, location,
    description and structured facts. ``enrichment`` carries the module 2
    shape, so the row is inserted already enriched (``module2_attempted``,
    ``module2_enriched_at`` and ``module2_model`` set) and the module 2 scraper
    never touches it.

``metadata_json`` contract for an enriched row (what module 3's job bundle
reads, see module3/job_bundle.py): a flat-ish JSON object of job facts, the
same shape module 2's Ollama step writes (``employment_type``,
``responsibilities``, ..., ``description_full``). Keys that start with ``_``
are jobo bookkeeping, not job facts:

  * ``_sources``: list of source names this job was seen in (dedup seen-in
    list, maintained by sources/store.py and preserved by module2/persist.py).
  * ``_jobfound``: jobfound provenance (id, page URL, raw category tags,
    published time), see sources/jobfound.py.

Structured filter inputs (read by policy.job_filters.facts_from_metadata) live
under ``_facts`` so that every source can provide them under one name:
``{"job_type": str|None, "categories": [str], "salary": str|None,
"experience": str|None}``. A source that does not know a value leaves it
None/empty and the matching rule is skipped for that row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sources import urls

SOURCES_KEY = "_sources"
FACTS_KEY = "_facts"


@dataclass
class JobCandidate:
    job_url: str
    source: str  # stored as jobs.source_platform ("linkedin", "jobfound", ...)
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    location: Optional[str] = None
    date_released: Optional[datetime] = None
    # Pre-enriched sources: the module 2 job_metadata dict (becomes
    # metadata_json) and the label stored in module2_model. None for bare links.
    enrichment: Optional[dict[str, Any]] = None
    enrichment_model: Optional[str] = None
    enrichment_error: Optional[str] = None  # e.g. thin-description fallback failed
    # Stable id inside the source (jobfound hex id); used by the source's own
    # seen-ledger, not by cross-source dedup.
    source_key: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def linkedin_job_id(self) -> Optional[str]:
        return urls.linkedin_job_id(self.job_url)

    @property
    def pre_enriched(self) -> bool:
        return self.enrichment is not None
