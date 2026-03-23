import json
import logging
import os
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
                    "Ollama HTTP %s %s — %s",
                    r.status_code,
                    url,
                    preview or "(empty body)",
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
        logger.warning("Ollama request failed: %s", e)
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
    """One retry with a fix-JSON hint if the first response does not validate."""
    out, err, raw = generate_json_enrichment(system_prompt, user_prompt, timeout=timeout)
    if out is not None:
        return out, None, raw

    fix_user = (
        user_prompt
        + "\n\nYour previous output was invalid. Reply with one JSON object only, "
        "no markdown, with keys company_name, job_title, location, date_released, "
        "job_metadata (object). Previous (broken) output:\n"
        + raw[:8000]
    )
    out2, err2, raw2 = generate_json_enrichment(system_prompt, fix_user, timeout=timeout)
    if out2 is not None:
        return out2, None, raw2
    return None, err or err2, raw2 or raw
