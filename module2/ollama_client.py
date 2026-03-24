import json
import logging
import os
import time
from typing import Optional

import httpx
from pydantic import ValidationError

from module2.enrichment_schema import EnrichmentOutput, parse_enrichment_json

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")


def _ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "gemma3:4b")


def get_ollama_model() -> str:
    return _ollama_model()


def generate_json_enrichment(
    system_prompt: str,
    user_prompt: str,
    *,
    timeout: float = 120.0,
) -> tuple[Optional[EnrichmentOutput], Optional[str], str]:
    """
    Call Ollama /api/generate with format=json.
    Returns (parsed_output, error_message, raw_response_text).
    On success error_message is None.
    """
    model = _ollama_model()
    url = f"{_ollama_host()}/api/generate"
    body = {
        "model": model,
        "prompt": f"{system_prompt}\n\n{user_prompt}",
        "stream": False,
        "format": "json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=body)
            if not r.is_success:
                preview = (r.text or "")[:800].replace("\n", " ")
                logger.warning(
                    f"Ollama HTTP {r.status_code} {url} — {preview or '(empty body)'}",
                )
                return (
                    None,
                    f"Ollama HTTP {r.status_code}: {preview[:200]}",
                    r.text or "",
                )
            try:
                payload = r.json()
            except json.JSONDecodeError as e:
                return None, f"Ollama invalid JSON body: {e}", r.text or ""
    except httpx.HTTPError as e:
        logger.warning(f"Ollama request failed: {e}")
        return None, f"Ollama HTTP error: {e}", ""

    raw = (payload.get("response") or "").strip()
    if not raw:
        return None, "Ollama returned empty response", raw

    try:
        out = parse_enrichment_json(raw)
        return out, None, raw
    except (json.JSONDecodeError, ValueError, ValidationError) as e:
        return None, f"Parse error: {e}", raw


def generate_json_enrichment_with_retry(
    system_prompt: str,
    user_prompt: str,
    *,
    timeout: float = 120.0,
) -> tuple[Optional[EnrichmentOutput], Optional[str], str]:
    """Up to 3 identical Ollama calls; 2s then 4s pause before 2nd and 3rd attempt."""
    last_err: Optional[str] = None
    last_raw = ""
    backoff_s = (2.0, 4.0)

    for attempt in range(3):
        if attempt > 0:
            delay = backoff_s[attempt - 1]
            logger.info(
                "Ollama enrichment failed (%s); pausing %.0fs before attempt %s/3",
                last_err or "unknown",
                delay,
                attempt + 1,
            )
            time.sleep(delay)

        out, err, raw = generate_json_enrichment(
            system_prompt, user_prompt, timeout=timeout
        )
        last_raw = raw
        last_err = err
        if out is not None:
            return out, None, raw

    return None, last_err, last_raw
