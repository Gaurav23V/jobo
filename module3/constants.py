"""Tunable defaults for Module 3 (jobo triage)."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FIT_SCORE_MIN_APPLY = 3

# The profile and base resume live in the candidate's local, gitignored
# personal/ directory (policy/personal.py). The env vars below still win, but
# only when they name a file that exists: an older .env that points at the
# removed module3/templates/resume.tex falls through to personal/.
DEFAULT_ARTIFACTS_REL = Path("data/module3/artifacts")


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


def profile_context_path() -> Path:
    from policy import personal

    p = _env_path("JOBO_PROFILE_CONTEXT_PATH")
    return p if p is not None and p.is_file() else personal.profile_path()


def artifacts_dir() -> Path:
    raw = os.environ.get("JOBO_ARTIFACTS_DIR", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return REPO_ROOT / DEFAULT_ARTIFACTS_REL


def min_fit_score_apply() -> int:
    raw = os.environ.get("JOBO_MIN_FIT_SCORE", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return FIT_SCORE_MIN_APPLY


def base_resume_tex_path() -> Path | None:
    from policy import personal

    p = _env_path("JOBO_BASE_RESUME_TEX")
    if p is not None and p.is_file():
        return p
    q = personal.resume_tex_path()
    return q if q.is_file() else p


def default_resume_pdf_path() -> Path | None:
    from policy import personal

    p = _env_path("JOBO_DEFAULT_RESUME_PDF")
    if p is not None and p.is_file():
        return p
    q = personal.resume_pdf_path()
    return q if q.is_file() else p
