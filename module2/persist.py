import json
from datetime import datetime
from typing import Optional

from dateutil import parser as date_parser
from sqlalchemy.orm import Session

from db.models import JobModel
from module2.enrichment_schema import EnrichmentOutput

_MODULE2_LAST_ERROR_MAX_LEN = 10_000


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


def _format_module2_last_error(
    message: str,
    raw_response_preview: Optional[str] = None,
) -> str:
    message = (message or "").strip()
    preview = (raw_response_preview or "").strip()
    if preview:
        text = f"{message}\n\n---\n\n{preview}"
    else:
        text = message
    if len(text) > _MODULE2_LAST_ERROR_MAX_LEN:
        return text[: _MODULE2_LAST_ERROR_MAX_LEN] + "…"
    return text


def save_enrichment_result(
    session: Session,
    job: JobModel,
    *,
    enrichment: Optional[EnrichmentOutput],
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
    raw_response_preview: Optional[str] = None,
) -> None:
    """Persist Module 2 outcome.

    Success: ``metadata_json`` is replaced with the job-only ``job_metadata`` object
    as JSON; sets ``module2_enriched_at``, ``module2_model``, clears
    ``module2_last_error``.

    Failure: leaves ``metadata_json`` unchanged; sets ``module2_last_error``;
    does not update ``module2_enriched_at`` / ``module2_model`` (last success kept).
    """
    job.module2_attempted = True

    if enrichment is None:
        if error_message:
            job.module2_last_error = _format_module2_last_error(
                error_message, raw_response_preview
            )
        session.add(job)
        session.commit()
        return

    job.metadata_json = json.dumps(
        enrichment.job_metadata,
        default=str,
        ensure_ascii=False,
    )
    job.module2_enriched_at = datetime.utcnow()
    job.module2_model = (model_name or "").strip() or None
    job.module2_last_error = None

    job.company_name = _apply_column_rule(job.company_name, enrichment.company_name)
    job.job_title = _apply_column_rule(job.job_title, enrichment.job_title)
    job.location = _apply_column_rule(job.location, enrichment.location)
    parsed_dr = _parse_date_released(enrichment.date_released)
    if parsed_dr is not None and job.date_released is None:
        job.date_released = parsed_dr

    session.add(job)
    session.commit()
