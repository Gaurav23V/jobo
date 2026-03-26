"""Tunable defaults for Module 3 (jobo triage)."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FIT_SCORE_MIN_APPLY = 3

DEFAULT_PROFILE_REL = Path("module3/context/profile.md")
DEFAULT_ARTIFACTS_REL = Path("data/module3/artifacts")


def profile_context_path() -> Path:
    raw = os.environ.get("JOBO_PROFILE_CONTEXT_PATH", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return REPO_ROOT / DEFAULT_PROFILE_REL


def artifacts_dir() -> Path:
    raw = os.environ.get("JOBO_ARTIFACTS_DIR", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return REPO_ROOT / DEFAULT_ARTIFACTS_REL


def gemini_model() -> str:
    return os.environ.get("JOBO_GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL


def min_fit_score_apply() -> int:
    raw = os.environ.get("JOBO_MIN_FIT_SCORE", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return FIT_SCORE_MIN_APPLY


def base_resume_tex_path() -> Path | None:
    raw = os.environ.get("JOBO_BASE_RESUME_TEX", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p)


def default_resume_pdf_path() -> Path | None:
    raw = os.environ.get("JOBO_DEFAULT_RESUME_PDF", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p)
