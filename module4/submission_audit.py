"""Durable per-attempt submission audit trail for Module 4 (apply).

the user asked directly: "how do we know what was actually filled into these job
postings?" The live application form is gone the moment the browser
navigates away, so the only source of truth is the agent's own report at the
end of its session. This module persists that report to disk in a form
that's cheap to query later:

  data/logs/apply_audit/job<id>_<timestamp>.json

Each record captures the agent's self-reported FIELDS_FILLED_JSON (see
module4/prompts.py's mandatory final-answer contract), plus which resume/
cover PDF paths were used and their content hashes (so a later resume-
template fix can be cross-referenced against "which submitted applications
used the OLD resume" the same way module3/manifest.py's hash lets you do it
for materials generation).

IMPORTANT CAVEAT (documented, not silently glossed over): this is the agent's
self-report, not independently verified against the live page. An agent can
mis-transcribe a value it actually typed, or (rarely) hallucinate a field it
never touched. Treat this as strong circumstantial evidence for "what was
probably submitted," not as ground truth on the level of a server-side
confirmation email. For anything the user needs bulletproof certainty on, the
company's own confirmation email/portal is still the authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIT_DIR = Path(__file__).parent.parent / "data" / "logs" / "apply_audit"


def _sha256_file(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:
        return None


def write_audit_record(
    *,
    job_id: int,
    company: str,
    title: str,
    url: str,
    status: str,
    detail: str,
    fields_filled: dict,
    resume_path: str,
    cover_path: str | None,
    attempt_number: int,
    resume_evidence: dict | None = None,
) -> Path | None:
    """Best-effort: never raises into the caller, just logs and returns None.

    resume_evidence (TODO item 18): what the DRIVER verified, as opposed to
    the agent's self-report: the job's upload file name, verified resume
    uploads, whether an Easy Apply modal was seen and our card verified as
    selected, and any resume-rule violation.
    """
    try:
        record = {
            "job_id": job_id,
            "company": company,
            "title": title,
            "url": url,
            "attempt_number": attempt_number,
            "recorded_at": datetime.now().isoformat(),
            "status": status,
            "detail": detail,
            "fields_filled_by_agent": fields_filled,
            "resume_pdf_path": resume_path,
            "resume_pdf_hash": _sha256_file(resume_path),
            "cover_pdf_path": cover_path,
            "cover_pdf_hash": _sha256_file(cover_path) if cover_path else None,
            "resume_evidence": resume_evidence or {},
        }
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = AUDIT_DIR / f"job{job_id}_{stamp}.json"
        out_path.write_text(json.dumps(record, indent=2))
        return out_path
    except Exception as e:  # pragma: no cover - audit trail must never block a run
        logger.warning("Failed to write submission audit record for job %s: %s", job_id, e)
        return None
