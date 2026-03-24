"""Serialize a job row into prompt-safe text."""

import json
from typing import Any

from db.models import JobModel


def build_job_bundle_text(job: JobModel) -> str:
    meta: Any
    try:
        meta = json.loads(job.metadata_json or "{}")
    except json.JSONDecodeError:
        meta = {"_parse_error": "invalid metadata_json", "raw": job.metadata_json}
    meta_str = json.dumps(meta, indent=2, ensure_ascii=False)

    lines = [
        f"job_id: {job.id}",
        f"company_name: {job.company_name or ''}",
        f"job_title: {job.job_title or ''}",
        f"location: {job.location or ''}",
        f"job_url: {job.job_url}",
        f"source_platform: {job.source_platform or ''}",
        "",
        "metadata_json (enriched job posting):",
        meta_str,
    ]
    return "\n".join(lines)
