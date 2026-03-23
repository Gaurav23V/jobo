import json
from datetime import datetime
from typing import Any, Optional

from dateutil import parser as date_parser
from sqlalchemy.orm import Session

from db.models import JobModel
from module2.enrichment_schema import EnrichmentOutput


def _merge_metadata(existing_json: str, module2_patch: dict[str, Any]) -> str:
    try:
        meta = json.loads(existing_json) if existing_json else {}
    except json.JSONDecodeError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    prev_m2 = meta.get("module2")
    if isinstance(prev_m2, dict):
        m2 = {**prev_m2, **module2_patch}
    else:
        m2 = dict(module2_patch)
    meta["module2"] = m2
    return json.dumps(meta, default=str)


def _parse_date_released(value: Optional[str]) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    try:
        return date_parser.parse(value, fuzzy=True)
    except (ValueError, TypeError):
        return None


def _apply_column_rule(current: Optional[str], incoming: Optional[str]) -> Optional[str]:
    """Fill empty columns; overwrite only when incoming is non-empty."""
    if incoming is None or (isinstance(incoming, str) and not incoming.strip()):
        return current
    inc = incoming.strip() if isinstance(incoming, str) else str(incoming)
    if current is None or (isinstance(current, str) and not current.strip()):
        return inc
    return inc


def save_enrichment_result(
    session: Session,
    job: JobModel,
    *,
    enrichment: Optional[EnrichmentOutput],
    module2_meta: dict[str, Any],
) -> None:
    """Merge enrichment into job, merge module2 metadata, set module2_attempted, commit."""
    module2_meta = dict(module2_meta)
    if enrichment is None:
        job.metadata_json = _merge_metadata(job.metadata_json, module2_meta)
        job.module2_attempted = True
        session.add(job)
        session.commit()
        return

    extra = enrichment.model_dump(exclude_none=True)
    known = {
        "company_name",
        "job_title",
        "location",
        "date_released",
        "employment_type",
        "remote_policy",
        "summary",
        "requirements_bullets",
    }
    for k, v in extra.items():
        if k in known:
            continue
        module2_meta.setdefault(k, v)

    job.company_name = _apply_column_rule(job.company_name, enrichment.company_name)
    job.job_title = _apply_column_rule(job.job_title, enrichment.job_title)
    job.location = _apply_column_rule(job.location, enrichment.location)
    parsed_dr = _parse_date_released(enrichment.date_released)
    if parsed_dr is not None:
        if job.date_released is None:
            job.date_released = parsed_dr
    elif enrichment.date_released and enrichment.date_released.strip():
        module2_meta.setdefault("date_released_raw", enrichment.date_released)

    job.metadata_json = _merge_metadata(job.metadata_json, module2_meta)
    job.module2_attempted = True
    session.add(job)
    session.commit()
