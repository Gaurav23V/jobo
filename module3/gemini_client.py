"""Gemini API: structured JSON via google-genai."""

from __future__ import annotations

import logging
import os
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
    """Call Gemini with JSON schema; retry once on empty/invalid JSON."""
    client = _client()
    schema = response_model.model_json_schema()
    fix_suffix = (
        "\n\nYour previous reply was empty or invalid JSON. "
        "Reply with one JSON object only, matching the schema."
    )

    payloads: list[tuple[str, str, bool]] = [
        (system_instruction, user_text, True),
        (system_instruction, user_text + fix_suffix, True),
        (system_instruction, user_text + fix_suffix, False),
    ]

    last_err: str | None = None
    for si, ut, use_si_field in payloads:
        try:
            if use_si_field:
                config = types.GenerateContentConfig(
                    system_instruction=si,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                )
                contents = ut
            else:
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                )
                contents = f"SYSTEM INSTRUCTIONS:\n{si}\n\nUSER:\n{ut}"
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            raw = (resp.text or "").strip()
        except Exception as e:
            last_err = str(e)
            logger.warning("Gemini request failed: %s", e)
            continue
        if not raw:
            last_err = "empty response text"
            continue
        try:
            return _parse_or_raise(raw, response_model)
        except (ValidationError, ValueError) as e:
            last_err = f"parse: {e}"
            logger.warning("Gemini JSON parse failed: %s", e)

    raise RuntimeError(f"Gemini structured call failed after retries: {last_err}")
