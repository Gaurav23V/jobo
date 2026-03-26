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

MODELS = ("gemini-3.1-pro-preview", "gemini-3-flash-preview")
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
    system_instruction: str,
    user_text: str,
    response_model: type[T],
) -> T:
    """Call Gemini with JSON schema; retry up to 3 times with exponential backoff (60s, 120s, 240s) across two models."""
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
        for model in MODELS:
            logger.info("Attempt %s/%s: model=%s", attempt + 1, max_attempts, model)
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=user_text,
                    config=config,
                )
                raw = (resp.text or "").strip()
            except Exception as e:
                last_err = f"{model}: {e}"
                logger.warning("Gemini request failed: %s", e)
                break
            if not raw:
                last_err = f"{model}: empty response"
                logger.warning("Gemini empty response")
                break
            try:
                return _parse_or_raise(raw, response_model)
            except (ValidationError, ValueError) as e:
                last_err = f"{model}: parse: {e}"
                logger.warning("Gemini JSON parse failed: %s", e)
                break

        if attempt < max_attempts - 1:
            delay = backoff_sec[attempt]
            logger.info(
                "Retry %s/%s after %ss backoff", attempt + 2, max_attempts, delay
            )
            time.sleep(delay)

    raise RuntimeError(f"Gemini structured call failed after retries: {last_err}")
