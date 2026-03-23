import logging
from dataclasses import dataclass, field

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

SYSTEM_PROMPT = """You transform job posting plain text into exactly one JSON object. No markdown fences, no commentary.

Top-level keys:
- company_name (string or null)
- job_title (string or null)
- location (string or null)
- date_released (string or null): ISO 8601 calendar date only, e.g. "2026-03-15", if and only if an explicit posting or release date appears in the input text. If the text only has relative phrases (e.g. "3 days ago") or no date, use null. Never invent a date.
- job_metadata (object, required): All substantive content from the Description section must live here. You may nest objects and arrays freely (sections, bullets, key-value pairs). Employment type, remote/hybrid/onsite, responsibilities, requirements, company blurb, etc. belong in job_metadata when they appear in the description.

Rules:
1. Do not fabricate: only use facts supported by the provided text.
2. Do not summarize or omit information: preserve the full substance of the Description—reorganize and clean whitespace/typos only; keep lists and paragraphs.
3. Use null for top-level string fields when that information is not present in the input (empty string is not required; null is fine).
4. Align company_name, job_title, and location with the labeled Title/Company/Location lines when they exist; extended or duplicate detail may also appear inside job_metadata."""


@dataclass
class EnrichResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_enrich(session: Session, *, dry_run: bool = False, force: bool = False) -> EnrichResult:
    result = EnrichResult()
    jobs = list_jobs_for_enrichment(session, force=force)
    logger.info(
        f"Enrich: {len(jobs)} job(s) to process (force={force}, dry_run={dry_run})",
    )

    if not jobs:
        return result

    with playwright_browser_context() as context:
        for job in jobs:
            enrich_delay_jitter()
            result.attempted += 1
            logger.info(
                f"job_id={job.id} browser extraction starting url={job.job_url}",
            )
            ext = extract_linkedin_job(context, job.job_url)

            if ext.error:
                msg = f"job_id={job.id} url={job.job_url} scrape: {ext.error}"
                logger.warning(msg)
                result.failed += 1
                if dry_run:
                    logger.info(f"[dry-run] Would persist scrape failure: {ext.error}")
                else:
                    save_enrichment_result(
                        session,
                        job,
                        enrichment=None,
                        error_message=f"scrape: {ext.error}",
                    )
                continue

            logger.info(
                f"job_id={job.id} browser extraction done url={job.job_url} "
                f"title={ext.title!r} company={ext.company!r}",
            )

            user = (
                "Transform the following job posting text into JSON as instructed. "
                "Put the entire Description body into job_metadata without dropping content.\n\n"
                + ext.to_llm_blob()
            )
            logger.info(
                f"job_id={job.id} Ollama enrichment starting url={job.job_url} "
                f"model={get_ollama_model()}",
            )
            enrichment, llm_err, raw = generate_json_enrichment_with_retry(
                SYSTEM_PROMPT, user
            )

            if llm_err:
                result.failed += 1
                result.errors.append(
                    f"job_id={job.id} url={job.job_url} llm: {llm_err}"
                )
                logger.warning(
                    f"job_id={job.id} Ollama failed url={job.job_url} — {llm_err}",
                )
                if dry_run:
                    logger.info(f"[dry-run] Would persist LLM failure: {llm_err}")
                else:
                    save_enrichment_result(
                        session,
                        job,
                        enrichment=None,
                        error_message=str(llm_err),
                        raw_response_preview=raw[:2000] if raw else None,
                    )
                continue

            if dry_run:
                logger.info(
                    f"[dry-run] Would persist job_id={job.id} url={job.job_url} "
                    f"model={get_ollama_model()} enrichment="
                    f"{enrichment.model_dump() if enrichment else None}",
                )
                result.succeeded += 1
            else:
                save_enrichment_result(
                    session,
                    job,
                    enrichment=enrichment,
                    model_name=get_ollama_model(),
                )
                result.succeeded += 1
            logger.info(
                f"job_id={job.id} enrich finished ok url={job.job_url}",
            )

    return result
