"""Per-application audit manifest for Module 3 (resume/cover materials).

Writes a small JSON file alongside each job's generated resume/cover PDFs
capturing exactly what template/profile version produced them. This exists
so a future base-template fix (like the 2026-09-01 missing-employer bug,
discovered only by manually diffing git history) can be traced automatically:
"which of my already-submitted applications used a resume that was missing
job X" becomes a one-line query over these manifests instead of an ad-hoc
forensic dig.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_manifest(
    *,
    art_dir: Path,
    job_id: int,
    company: str,
    title: str,
    base_resume_tex: str,
    profile_markdown: str,
    resume_needs_update: bool,
    resume_pdf_path: str,
    cover_pdf_path: str,
) -> Path:
    """Write manifest.json in art_dir. Returns its path."""
    manifest = {
        "job_id": job_id,
        "company": company,
        "title": title,
        "generated_at": datetime.now().isoformat(),
        "base_resume_tex_hash": _sha256(base_resume_tex),
        "candidate_profile_hash": _sha256(profile_markdown),
        "resume_tailored": resume_needs_update,
        "resume_pdf_path": resume_pdf_path,
        "cover_pdf_path": cover_pdf_path,
    }
    art_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = art_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path
