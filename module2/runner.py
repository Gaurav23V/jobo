import logging
import os
import queue
import threading
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import JobModel
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

_QUEUE_MAXSIZE = 8


@dataclass
class EnrichResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _num_workers() -> int:
    raw = os.environ.get("JOBO_ENRICH_WORKERS", "").strip()
    try:
        n = int(raw) if raw else 3
    except ValueError:
        n = 3
    return max(1, min(n, 8))


# Verbatim posting text kept next to the model's structured fields. The 3B
# model compresses long postings: on 8 rows sampled 2026-10-15 it kept 82-96%
# of the description's words for short postings but 44-62% for ones over
# ~3.5k chars (prompt ~1.9k tokens of a 32k window, so not a context cut;
# raising num_ctx did not change it). Triage scores fit from metadata_json,
# so it was judging some jobs on half the posting. The cap bounds the extra
# triage prompt tokens; most postings are 2-5k chars.
FULL_DESCRIPTION_KEY = "description_full"
FULL_DESCRIPTION_MAX_CHARS = 8000


def attach_full_description(job_metadata: dict, description: str | None) -> dict:
    text = (description or "").strip()
    if text:
        if len(text) > FULL_DESCRIPTION_MAX_CHARS:
            text = text[:FULL_DESCRIPTION_MAX_CHARS].rstrip() + " [...]"
        job_metadata[FULL_DESCRIPTION_KEY] = text
    return job_metadata


def _producer(q: "queue.Queue", jobs: list[JobModel]) -> None:
    """Owns Playwright exclusively (sync API is thread-bound). Pushes scrape results."""
    try:
        with playwright_browser_context() as context:
            for job in jobs:
                enrich_delay_jitter()
                logger.info(
                    f"job_id={job.id} browser extraction starting url={job.job_url}",
                )
                ext = extract_linkedin_job(context, job.job_url)
                q.put((job.id, ext))
        logger.info("Producer: all pages scraped")
    except Exception as e:
        logger.exception(f"Producer crashed: {e}")
        # Poison every worker so main does not hang.
        raise


def _worker(
    worker_id: int,
    q: "queue.Queue",
    *,
    dry_run: bool,
    result: EnrichResult,
    lock: threading.Lock,
) -> None:
    session = SessionLocal()
    try:
        while True:
            item = q.get()
            if item is None:
                break
            job_id, ext = item
            job = session.get(JobModel, job_id)
            if job is None:
                continue

            with lock:
                result.attempted += 1

            if ext.error:
                msg = f"job_id={job.id} url={job.job_url} scrape: {ext.error}"
                logger.warning(f"[w{worker_id}] {msg}")
                with lock:
                    result.failed += 1
                    result.errors.append(msg)
                if dry_run:
                    logger.info(f"[w{worker_id}][dry-run] Would persist scrape failure: {ext.error}")
                else:
                    save_enrichment_result(
                        session,
                        job,
                        enrichment=None,
                        error_message=f"scrape: {ext.error}",
                    )
                continue

            logger.info(
                f"[w{worker_id}] job_id={job.id} browser extraction done "
                f"title={ext.title!r} company={ext.company!r}",
            )

            user = (
                "Transform the following job posting text into JSON as instructed. "
                "Put the entire Description body into job_metadata without dropping content.\n\n"
                + ext.to_llm_blob()
            )
            logger.info(
                f"[w{worker_id}] job_id={job.id} Ollama enrichment starting model={get_ollama_model()}",
            )
            enrichment, llm_err, raw = generate_json_enrichment_with_retry(
                SYSTEM_PROMPT, user
            )

            if llm_err:
                msg = f"job_id={job.id} url={job.job_url} llm: {llm_err}"
                logger.warning(f"[w{worker_id}] Ollama failed — {llm_err}")
                with lock:
                    result.failed += 1
                    result.errors.append(msg)
                if not dry_run:
                    save_enrichment_result(
                        session,
                        job,
                        enrichment=None,
                        error_message=str(llm_err),
                        raw_response_preview=raw[:2000] if raw else None,
                    )
                continue

            # Prefer LLM values; fall back to the (more deterministic) scrape
            # when the model omitted a field.
            enrichment.company_name = enrichment.company_name or ext.company
            enrichment.job_title = enrichment.job_title or ext.title
            enrichment.location = enrichment.location or ext.location
            attach_full_description(enrichment.job_metadata, ext.description)

            if dry_run:
                logger.info(
                    f"[w{worker_id}][dry-run] Would persist job_id={job.id} "
                    f"enrichment={enrichment.model_dump()}",
                )
                with lock:
                    result.succeeded += 1
            else:
                save_enrichment_result(
                    session,
                    job,
                    enrichment=enrichment,
                    model_name=get_ollama_model(),
                )
                with lock:
                    result.succeeded += 1
            logger.info(f"[w{worker_id}] job_id={job.id} enrich finished ok")
    except Exception as e:
        logger.exception(f"[w{worker_id}] Worker crashed: {e}")
    finally:
        session.close()


def run_enrich(
    session: Session, *, dry_run: bool = False, force: bool = False, job_id: int | None = None
) -> EnrichResult:
    """Producer-consumer enrichment.

    One producer thread owns Playwright (scrape is network-bound); K worker
    threads run Ollama + persistence (compute-bound), overlapping the two.
    """
    result = EnrichResult()
    jobs = list_jobs_for_enrichment(session, force=force, job_id=job_id)
    num_workers = _num_workers()
    logger.info(
        f"Enrich: {len(jobs)} job(s) to process "
        f"(force={force}, dry_run={dry_run}, workers={num_workers})",
    )

    if not jobs:
        return result

    lock = threading.Lock()
    q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAXSIZE)

    producer = threading.Thread(
        target=_producer, args=(q, jobs), name="enrich-producer", daemon=True
    )
    workers = [
        threading.Thread(
            target=_worker,
            args=(i, q),
            kwargs={"dry_run": dry_run, "result": result, "lock": lock},
            name=f"enrich-worker-{i}",
            daemon=True,
        )
        for i in range(num_workers)
    ]

    for w in workers:
        w.start()
    producer.start()

    # Backpressure note: Queue.put blocks when full, so the producer naturally
    # pauses if the LLM workers fall behind.
    producer.join()
    for _ in workers:
        q.put(None)  # one sentinel per worker
    for w in workers:
        w.join(timeout=300)

    alive = [w.name for w in workers if w.is_alive()]
    if alive:
        logger.warning(f"Workers did not exit cleanly: {alive}")

    logger.info(
        f"Enrich done: attempted={result.attempted} ok={result.succeeded} "
        f"failed={result.failed}",
    )
    return result
