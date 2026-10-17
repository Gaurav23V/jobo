"""LLM via local Hermes CLI (one-shot `hermes chat`), replacing the Gemini API.

Contract: system + user text + Pydantic
response model -> validated instance. JSON is extracted from stdout and parsed
with a small retry loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_REASONING = "low"
DEFAULT_TIMEOUT_SEC = 300
MAX_ATTEMPTS = 3


def _hermes_bin() -> str:
    path = shutil.which("hermes")
    if not path:
        raise RuntimeError(
            "hermes CLI not found on PATH; install Hermes Agent first."
        )
    return path


def reasoning_effort() -> str:
    return os.environ.get("JOBO_HERMES_REASONING", "").strip() or DEFAULT_REASONING


def call_timeout() -> int:
    raw = os.environ.get("JOBO_HERMES_TIMEOUT_SEC", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_TIMEOUT_SEC


_JSON_BLOCK_RE = re.compile(r"[\[{].*[\]}]", re.DOTALL)

# A literal newline/tab inside a JSON string value (common when models emit
# multi-line LaTeX in a JSON field). Escape control chars that sit between
# unescaped quotes so json.loads can parse it.
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def repair_json(raw_json: str) -> str:
    """Escape bare control characters inside JSON string literals."""

    def _fix(m: re.Match) -> str:
        s = m.group(0)
        return s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    return _STRING_RE.sub(_fix, raw_json)


def extract_json(raw: str) -> str:
    """Pull the first JSON object/array out of CLI output."""
    text = (raw or "").strip()
    # Strip markdown fences if present.
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return m.group(0)
    return text


def generate_structured(
    *,
    system_instruction: str,
    user_text: str,
    response_model: type[T],
) -> T:
    """Call hermes one-shot; validate against response_model; retry on failure."""
    binary = _hermes_bin()
    prompt = (
        f"{system_instruction.strip()}\n\n"
        f"## Output contract\n"
        f'Respond with ONLY a single valid JSON object matching this schema: '
        f'{json.dumps(response_model.model_json_schema(), ensure_ascii=False)}\n'
        f"No markdown fences, no commentary before or after.\n\n"
        f"{user_text.strip()}"
    )

    last_err: Exception | str | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            proc = subprocess.run(
                [
                    binary,
                    "chat",
                    "--query-file",
                    "-",
                    "-Q",
                    "--reasoning",
                    reasoning_effort(),
                    "--no-restore-cwd",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=call_timeout(),
            )
            raw = proc.stdout or ""
            if proc.returncode != 0 and not raw.strip():
                raise RuntimeError(
                    f"hermes exited {proc.returncode}: {(proc.stderr or '')[:500]}"
                )
        except subprocess.TimeoutExpired as e:
            last_err = f"timeout after {call_timeout()}s"
            logger.warning("Hermes call timed out (attempt %s/%s)", attempt + 1, MAX_ATTEMPTS)
            continue

        candidate = extract_json(raw)
        try:
            return response_model.model_validate_json(candidate)
        except ValidationError:
            try:
                repaired = json.loads(repair_json(candidate))
                return response_model.model_validate(repaired)
            except (ValueError, ValidationError) as e2:
                last_err = e2
                snippet = candidate[:300]
                logger.warning(
                    "Hermes JSON validation failed (attempt %s/%s): %s | got: %s",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    e2,
                    snippet,
                )
        except Exception as e:  # noqa: BLE001 - keep retrying any parse error
            last_err = e
            logger.warning(
                "Hermes response unusable (attempt %s/%s): %s", attempt + 1, MAX_ATTEMPTS, e
            )

        time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"Hermes structured call failed after {MAX_ATTEMPTS} attempts: {last_err}")
