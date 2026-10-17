"""Pick the LLM backend for Module 3: Claude Code (default) or the old Hermes path.

JOBO_LLM_BACKEND=hermes switches back to `hermes chat` without a code change,
in case Claude Code is unavailable. The Hermes path ignores `model`.
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

from module3 import claude_client, hermes_client

T = TypeVar("T", bound=BaseModel)


def backend() -> str:
    return os.environ.get("JOBO_LLM_BACKEND", "").strip().lower() or "claude"


def fit_model() -> str | None:
    """Model for the fit call; defaults to JOBO_CLAUDE_MODEL / sonnet."""
    return os.environ.get("JOBO_CLAUDE_FIT_MODEL", "").strip() or None


def generate_structured(
    *,
    system_instruction: str,
    user_text: str,
    response_model: type[T],
    model: str | None = None,
) -> T:
    if backend() == "hermes":
        return hermes_client.generate_structured(
            system_instruction=system_instruction,
            user_text=user_text,
            response_model=response_model,
        )
    return claude_client.generate_structured(
        system_instruction=system_instruction,
        user_text=user_text,
        response_model=response_model,
        model=model,
    )
