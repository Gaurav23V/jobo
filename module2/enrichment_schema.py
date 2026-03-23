import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class EnrichmentOutput(BaseModel):
    """JSON from the LLM: top-level columns + free-form description-derived metadata."""

    company_name: Optional[str] = None
    job_title: Optional[str] = None
    location: Optional[str] = None
    date_released: Optional[str] = None
    job_metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}

    @field_validator(
        "company_name",
        "job_title",
        "location",
        "date_released",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("job_metadata", mode="before")
    @classmethod
    def job_metadata_as_dict(cls, v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        return {}


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
