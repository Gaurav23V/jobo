"""Give every uploaded file a name that says which job it belongs to.

Why (TODO item 18, 2026-10-11 run): 13 of 27 applications went out through
LinkedIn Easy Apply carrying LinkedIn's saved, pre-selected resume instead of
the job's own one. LinkedIn lists every resume it has ever stored by its upload
file name, and ours was called resume.pdf, exactly like several old saved ones
(the candidate's account held 90+ cards, among them "resume.pdf", "Resume.pdf" and
dozens of "<First>_<Last>_Resume.pdf"). Nothing, agent or code, could tell from the name
which card was ours. Worse, the uploader's "already attached" check looked for
the text "resume.pdf" in the page's Resume section, so a saved card with that
name read as proof that our file was attached.

Each job therefore uploads a byte-identical COPY of its own resume named

    <First>_<Last>_Resume_<job id>_<first 6 hex of its sha256>.pdf

(the name part comes from personal/candidate.json, see policy/personal.py)

Professional enough for a recruiter to see, unique per job, and the hash part
changes when the content does, so the same name always means the same bytes.
The cover letter gets the same treatment. The source file
(module3_resume_pdf_path) is never modified.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

from policy import personal

CANDIDATE_SLUG = personal.upload_slug()
KIND_LABEL = {"resume": "Resume", "cover": "Cover_Letter"}


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def copies_dir() -> Path:
    """Where the named copies live. JOBO_UPLOAD_COPY_DIR overrides (tests)."""
    raw = os.environ.get("JOBO_UPLOAD_COPY_DIR", "").strip()
    return Path(raw) if raw else Path(tempfile.gettempdir()) / "jobo-upload-copies"


def upload_name(job_id: int, source: str | Path, kind: str = "resume") -> str:
    """The file name a recruiter (and the guard) will see for this job's file."""
    src = Path(source)
    label = KIND_LABEL.get(kind, "Resume")
    suffix = src.suffix.lower() or ".pdf"
    return f"{CANDIDATE_SLUG}_{label}_{int(job_id)}_{_sha256(src)[:6]}{suffix}"


def is_upload_name(name: str, job_id: int | None = None) -> bool:
    jid = r"\d+" if job_id is None else str(int(job_id))
    return bool(re.fullmatch(
        rf"{CANDIDATE_SLUG}_(?:Resume|Cover_Letter)_{jid}_[0-9a-f]{{6}}\.\w+", name or ""))


def prepare(job_id: int, source: str | None, kind: str = "resume") -> str | None:
    """Path of a byte-identical copy of `source` carrying this job's upload name.

    Idempotent: a path that already is this job's named copy comes back as is.
    A missing source is returned unchanged (the caller reports it). If the copy
    cannot be made, the original path is returned with a warning, so a full
    disk degrades to the old behaviour instead of stopping the run; the Easy
    Apply guard still refuses Submit without a verified upload.
    """
    if not source:
        return source
    src = Path(source)
    if not src.is_file():
        return source
    try:
        name = upload_name(job_id, src, kind)
        if src.name == name:
            return str(src)
        dest_dir = copies_dir() / str(int(job_id))
        dest = dest_dir / name
        want = _sha256(src)
        if not (dest.is_file() and _sha256(dest) == want):
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
            if _sha256(dest) != want:
                raise OSError(f"copy of {src} is not byte-identical")
        return str(dest)
    except Exception as e:
        logger.warning("[%s] Could not make the named %s copy of %s: %s", job_id, kind, src, e)
        return str(src)
