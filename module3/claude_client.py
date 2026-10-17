"""LLM via Claude Code one-shot (`claude -p`), replacing the Hermes harness.

Why not Hermes: Hermes's Claude provider already shells out to `claude -p` for
every model call, but wraps it in its own persona, skills index and tool
schemas (~25k tokens per call), and the model then wandered into those tools
(skill_view, read_file, terminal) on what is a pure text-in, JSON-out job.
Calling Claude Code directly with tools, settings, MCP and slash commands all
switched off leaves ~1k tokens of overhead, and --json-schema validates the
reply for us.

Contract (same as hermes_client): system + user text + Pydantic response
model -> validated instance, retried on failure.

Put the part of the prompt that is identical across jobs (instructions,
profile, base resume) in system_instruction: Claude Code caches the system
prompt, so parallel fit calls re-read it cheaply instead of paying full price
for it on every job.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from module3.hermes_client import extract_json, repair_json

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "low"
DEFAULT_TIMEOUT_SEC = 300
MAX_ATTEMPTS = 3


def _claude_bin() -> str:
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("claude CLI not found on PATH; install Claude Code first.")
    return path


def default_model() -> str:
    return os.environ.get("JOBO_CLAUDE_MODEL", "").strip() or DEFAULT_MODEL


def effort() -> str:
    return os.environ.get("JOBO_CLAUDE_EFFORT", "").strip() or DEFAULT_EFFORT


def call_timeout() -> int:
    raw = os.environ.get("JOBO_CLAUDE_TIMEOUT_SEC", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_TIMEOUT_SEC


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def summary(self) -> str:
        per_call = self.prompt_tokens // self.calls if self.calls else 0
        return (
            f"calls={self.calls} prompt_tokens={self.prompt_tokens} "
            f"(uncached={self.input_tokens} cache_read={self.cache_read_tokens} "
            f"cache_write={self.cache_write_tokens}) output={self.output_tokens} "
            f"avg_prompt_per_call={per_call}"
        )


# Running totals for this process. Fit calls run on a thread pool, hence the lock.
_usage = Usage()
_usage_lock = threading.Lock()


def usage_snapshot() -> Usage:
    with _usage_lock:
        return Usage(**vars(_usage))


def _record_usage(u: dict) -> None:
    with _usage_lock:
        _usage.calls += 1
        _usage.input_tokens += int(u.get("input_tokens") or 0)
        _usage.cache_read_tokens += int(u.get("cache_read_input_tokens") or 0)
        _usage.cache_write_tokens += int(u.get("cache_creation_input_tokens") or 0)
        _usage.output_tokens += int(u.get("output_tokens") or 0)


def build_command(*, system_prompt_file: Path, schema: dict, model: str) -> list[str]:
    return [
        _claude_bin(),
        "-p",
        "--model", model,
        "--effort", effort(),
        "--system-prompt-file", str(system_prompt_file),
        # No tools, no MCP servers, no user/project settings or CLAUDE.md, no
        # skills: this is a single structured answer, not an agent task.
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
    ]


def parse_response(stdout: str, response_model: type[T]) -> T:
    """Validate the `--output-format json` envelope against response_model."""
    envelope = json.loads(stdout)
    if not isinstance(envelope, dict):
        raise ValueError(f"unexpected claude output: {stdout[:300]}")
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        _record_usage(usage)
    if envelope.get("is_error"):
        raise RuntimeError(
            f"claude returned an error ({envelope.get('subtype')}): "
            f"{str(envelope.get('result'))[:500]}"
        )
    structured = envelope.get("structured_output")
    if structured is not None:
        return response_model.model_validate(structured)
    # No structured_output: fall back to parsing JSON out of the text result.
    candidate = extract_json(str(envelope.get("result") or ""))
    try:
        return response_model.model_validate_json(candidate)
    except ValidationError:
        return response_model.model_validate(json.loads(repair_json(candidate)))


def generate_structured(
    *,
    system_instruction: str,
    user_text: str,
    response_model: type[T],
    model: str | None = None,
) -> T:
    """Call `claude -p` once per attempt; validate against response_model; retry on failure."""
    schema = response_model.model_json_schema()
    model = model or default_model()

    last_err: Exception | str | None = None
    # A scratch dir doubles as cwd so no project CLAUDE.md or .claude/ is picked up.
    with tempfile.TemporaryDirectory(prefix="jobo-claude-") as tmp:
        system_file = Path(tmp) / "system.md"
        system_file.write_text(system_instruction.strip(), encoding="utf-8")
        cmd = build_command(system_prompt_file=system_file, schema=schema, model=model)

        for attempt in range(MAX_ATTEMPTS):
            try:
                proc = subprocess.run(
                    cmd,
                    input=user_text.strip(),
                    capture_output=True,
                    text=True,
                    timeout=call_timeout(),
                    cwd=tmp,
                )
                if proc.returncode != 0 and not (proc.stdout or "").strip():
                    raise RuntimeError(
                        f"claude exited {proc.returncode}: {(proc.stderr or '')[:500]}"
                    )
                return parse_response(proc.stdout or "", response_model)
            except subprocess.TimeoutExpired:
                last_err = f"timeout after {call_timeout()}s"
                logger.warning(
                    "Claude call timed out (attempt %s/%s)", attempt + 1, MAX_ATTEMPTS
                )
            except Exception as e:  # noqa: BLE001 - keep retrying any failure
                last_err = e
                logger.warning(
                    "Claude call failed (attempt %s/%s): %s", attempt + 1, MAX_ATTEMPTS, e
                )
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"Claude structured call failed after {MAX_ATTEMPTS} attempts: {last_err}")
