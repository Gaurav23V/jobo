"""Gemini API: structured JSON via google-genai."""

from __future__ import annotations

import logging
import os
import time
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
        )
    return genai.Client(api_key=key)


def _parse_or_raise(raw: str, response_model: type[T]) -> T:
    return response_model.model_validate_json(raw)


def generate_structured(
    *,
    model: str,
    system_instruction: str,
    user_text: str,
    response_model: type[T],
) -> T:
    """Call Gemini with JSON schema; retry up to 3 times with exponential backoff (60s, 120s, 240s)."""
    client = _client()
    schema = response_model.model_json_schema()
    max_attempts = 4
    backoff_sec = (60, 120, 240)

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_json_schema=schema,
    )

    last_err: str | None = None
    for attempt in range(max_attempts):
        if attempt > 0:
            delay = backoff_sec[attempt - 1]
            logger.info(
                "Gemini structured call retry %s/%s after %s s backoff",
                attempt + 1,
                max_attempts,
                delay,
            )
            time.sleep(delay)
        try:
            resp = client.models.generate_content(
                model=model,
                contents=user_text,
                config=config,
            )
            raw = (resp.text or "").strip()
        except Exception as e:
            last_err = str(e)
            logger.warning(
                "Gemini request failed (attempt %s/%s): %s",
                attempt + 1,
                max_attempts,
                e,
            )
            continue
        if not raw:
            last_err = "empty response text"
            logger.warning(
                "Gemini empty response (attempt %s/%s)",
                attempt + 1,
                max_attempts,
            )
            continue
        try:
            return _parse_or_raise(raw, response_model)
        except (ValidationError, ValueError) as e:
            last_err = f"parse: {e}"
            logger.warning(
                "Gemini JSON parse failed (attempt %s/%s): %s",
                attempt + 1,
                max_attempts,
                e,
            )

    raise RuntimeError(f"Gemini structured call failed after retries: {last_err}")
