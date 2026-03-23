import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from module2.linkedin_fetch import (
    enrich_delay_jitter,
    extract_linkedin_job,
    playwright_browser_context,
)
from module2.ollama_client import generate_json_enrichment_with_retry, get_ollama_model
from module2.persist import save_enrichment_result
from module2.query import list_jobs_for_enrichment

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract structured job data from plain text. Reply with one JSON object only, no markdown.
Keys: company_name, job_title, location, date_released (ISO 8601 date string or null),
employment_type, remote_policy, summary, requirements_bullets (array of strings, can be empty).
Use null for unknown values."""


@dataclass
class EnrichResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_enrich(session: Session, *, dry_run: bool = False, force: bool = False) -> EnrichResult:
    result = EnrichResult()
    jobs = list_jobs_for_enrichment(session, force=force)
    logger.info(
        "Enrich: %d job(s) to process (force=%s, dry_run=%s)",
        len(jobs),
        force,
        dry_run,
    )

    if not jobs:
        return result

    with playwright_browser_context() as context:
        for job in jobs:
            enrich_delay_jitter()
            result.processed += 1
            ext = extract_linkedin_job(context, job.job_url)

            if ext.error:
                msg = f"job_id={job.id} scrape: {ext.error}"
                logger.warning(msg)
                result.failed += 1
                meta = {
                    "status": "scrape_error",
                    "message": ext.error,
                    "enriched_at": datetime.now(timezone.utc).isoformat(),
                    "model": get_ollama_model(),
                }
                if dry_run:
                    logger.info("[dry-run] Would persist scrape failure: %s", meta)
                else:
                    save_enrichment_result(
                        session, job, enrichment=None, module2_meta=meta
                    )
                continue

            user = (
                "Extract structured fields from this job posting text.\n\n"
                + ext.to_llm_blob()
            )
            enrichment, llm_err, raw = generate_json_enrichment_with_retry(
                SYSTEM_PROMPT, user
            )
            base_meta = {
                "enriched_at": datetime.now(timezone.utc).isoformat(),
                "model": get_ollama_model(),
            }

            if llm_err:
                result.failed += 1
                result.errors.append(f"job_id={job.id} llm: {llm_err}")
                meta = {
                    **base_meta,
                    "status": "llm_error",
                    "message": llm_err,
                    "raw_response_preview": raw[:2000],
                }
                if dry_run:
                    logger.info("[dry-run] Would persist LLM failure: %s", meta)
                else:
                    save_enrichment_result(
                        session, job, enrichment=None, module2_meta=meta
                    )
                continue

            if dry_run:
                logger.info(
                    "[dry-run] Would persist job_id=%s enrichment=%s meta=%s",
                    job.id,
                    enrichment.model_dump() if enrichment else None,
                    base_meta,
                )
                result.succeeded += 1
            else:
                save_enrichment_result(
                    session, job, enrichment=enrichment, module2_meta=base_meta
                )
                result.succeeded += 1

    return result
