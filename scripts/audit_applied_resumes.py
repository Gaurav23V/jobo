#!/usr/bin/env python3
"""Audit already-submitted jobo applications against the CURRENT base resume
template. Prints every applied job whose resume PDF content differs from
what today's base template would produce for an untailored (resume_needs_
update=false) application -- i.e. jobs whose materials predate a base-
template fix like the 2026-09-01 missing-Demo Works-Tech bug.

Usage: uv run python scripts/audit_applied_resumes.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def sha256_file(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main() -> None:
    db_path = REPO_ROOT / "data" / "jobs.db"
    from module3 import constants as m3c

    base_tex_path = m3c.base_resume_tex_path()
    current_base_hash = sha256_text(base_tex_path.read_text(encoding="utf-8"))

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        """
        select id, company_name, job_title, module3_resume_pdf_path,
               module4_submitted_at
        from jobs
        where applied = 1
        order by module4_submitted_at
        """
    )
    rows = cur.fetchall()

    stale = []
    no_manifest = []
    current = []

    for jid, company, title, resume_path, submitted_at in rows:
        manifest_path = REPO_ROOT / "data" / "module3" / "artifacts" / str(jid) / "manifest.json"
        if not manifest_path.is_file():
            no_manifest.append((jid, company, title, submitted_at))
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            no_manifest.append((jid, company, title, submitted_at))
            continue
        base_hash_at_generation = manifest.get("base_resume_tex_hash")
        if base_hash_at_generation != current_base_hash:
            stale.append((jid, company, title, submitted_at, base_hash_at_generation))
        else:
            current.append((jid, company, title, submitted_at))

    print(f"Current base resume.tex hash: {current_base_hash}")
    print(f"Total applied jobs: {len(rows)}")
    print(f"  Generated from CURRENT base template: {len(current)}")
    print(f"  Generated from an OLDER/DIFFERENT base template (STALE): {len(stale)}")
    print(f"  No manifest found (predates manifest tracking, 2026-09-01): {len(no_manifest)}")
    print()

    if stale:
        print("=== STALE (base template changed since these were generated) ===")
        for jid, company, title, submitted_at, old_hash in stale:
            print(f"  job_id={jid} {company!r} / {title!r} submitted={submitted_at} old_base_hash={old_hash}")
        print()

    if no_manifest:
        print(
            f"=== NO MANIFEST ({len(no_manifest)} jobs, all applied before manifest "
            "tracking was added 2026-09-01) ==="
        )
        print(
            "These predate the audit system entirely - cannot be verified "
            "automatically. Known issue as of 2026-09-01: the base resume.tex "
            "template was missing the Demo Works role and showed Sample Labs as "
            "still ongoing ('Present') for ALL of these, until fixed same day."
        )
        for jid, company, title, submitted_at in no_manifest[:10]:
            print(f"  job_id={jid} {company!r} / {title!r} submitted={submitted_at}")
        if len(no_manifest) > 10:
            print(f"  ... and {len(no_manifest) - 10} more")


if __name__ == "__main__":
    main()
