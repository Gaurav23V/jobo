import json
import re
from typing import Any, Optional

from pydantic import BaseModel, field_validator


class EnrichmentOutput(BaseModel):
    """Expected JSON shape from the LLM (flexible extras allowed)."""

    company_name: Optional[str] = None
    job_title: Optional[str] = None
    location: Optional[str] = None
    date_released: Optional[str] = None
    employment_type: Optional[str] = None
    remote_policy: Optional[str] = None
    summary: Optional[str] = None
    requirements_bullets: Optional[list[str]] = None

    model_config = {"extra": "allow"}

    @field_validator(
        "company_name",
        "job_title",
        "location",
        "date_released",
        "employment_type",
        "remote_policy",
        "summary",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE
)


def strip_json_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def parse_enrichment_json(text: str) -> EnrichmentOutput:
    cleaned = strip_json_fences(text)
    data = json.loads(cleaned)
    return EnrichmentOutput.model_validate(data)
