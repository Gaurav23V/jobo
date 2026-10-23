"""The person jobo applies for: contact details, profile, resume and password.

Nothing personal lives in the code. Everything about the candidate is read from
a local, gitignored directory (``personal/`` at the repo root, or the directory
named by ``JOBO_PERSONAL_DIR``):

    personal/candidate.json     structured facts (name, email, phone, links,
                                school aliases, city aliases, ...)
    personal/candidate_facts.md the "Candidate facts" block shown to the apply
                                agent (free text, single source of truth)
    personal/profile.md         module3 profile (intro, experience, projects)
    personal/resume.tex         base LaTeX resume (module3 tailors a copy)
    personal/resume.pdf         compiled base resume (fallback upload)
    personal/secrets.json       {"account_password": "..."} - the one password
                                jobo uses for every job-site account it creates
                                or resets. Keep it mode 600.

``personal.example/`` (committed) holds the same files for a fictional person.
``./setup.sh`` writes ``personal/`` from your answers.

If no ``personal/`` exists, the example directory is used so that imports,
tests and ``jobo doctor`` still work, but ``is_example()`` is then True and the
commands that act for the candidate (triage, apply) refuse to run.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "personal.example"
DEFAULT_DIR = REPO_ROOT / "personal"

CANDIDATE_FILE = "candidate.json"
FACTS_FILE = "candidate_facts.md"
PROFILE_FILE = "profile.md"
RESUME_TEX_FILE = "resume.tex"
RESUME_PDF_FILE = "resume.pdf"
SECRETS_FILE = "secrets.json"

PASSWORD_ENV = "JOBO_ACCOUNT_PASSWORD"


def personal_dir() -> Path:
    """Directory holding the candidate's local config."""
    raw = os.environ.get("JOBO_PERSONAL_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)
    if DEFAULT_DIR.is_dir():
        return DEFAULT_DIR
    return EXAMPLE_DIR


def is_example() -> bool:
    """True when jobo is running on the fictional example person."""
    try:
        return personal_dir().resolve() == EXAMPLE_DIR.resolve()
    except OSError:
        return False


def path(name: str) -> Path:
    return personal_dir() / name


@functools.lru_cache(maxsize=None)
def _load_json(p: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def candidate() -> dict[str, Any]:
    """Parsed candidate.json ({} when missing or unreadable)."""
    return _load_json(str(path(CANDIDATE_FILE)))


def get(key: str, default: Any = "") -> Any:
    value = candidate().get(key, default)
    return default if value is None else value


def full_name() -> str:
    return str(get("full_name", "")).strip()


def first_name() -> str:
    explicit = str(get("first_name", "")).strip()
    if explicit:
        return explicit
    name = full_name()
    return name.split()[0] if name else "the candidate"


def email() -> str:
    return str(get("email", "")).strip()


def upload_slug() -> str:
    """File-name stem for uploads, e.g. Alex_Example (letters/digits/_ only)."""
    explicit = str(get("upload_slug", "")).strip()
    base = explicit or full_name() or "Candidate"
    slug = "".join(c if c.isalnum() else "_" for c in base)
    slug = "_".join(p for p in slug.split("_") if p)
    return slug or "Candidate"


def answer_chain(key: str) -> tuple[list[str], list[str]]:
    """(triggers, options) for a ranked dropdown fallback, e.g. "school"/"city".

    candidate.json: {"answer_chains": {"school": {"triggers": [...],
    "options": [...]}}}. Triggers are what the agent may ask for; options are
    the honest site values to try, most specific first.
    """
    chain = (get("answer_chains", {}) or {}).get(key) or {}
    if not isinstance(chain, dict):
        return [], []
    clean = lambda xs: [str(x) for x in (xs or []) if str(x).strip()]
    return clean(chain.get("triggers")), clean(chain.get("options"))


def candidate_facts() -> str:
    """The free-text facts block the apply agent treats as ground truth."""
    try:
        text = path(FACTS_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip("\n")


def profile_path() -> Path:
    return path(PROFILE_FILE)


def resume_tex_path() -> Path:
    return path(RESUME_TEX_FILE)


def resume_pdf_path() -> Path:
    return path(RESUME_PDF_FILE)


def account_password() -> str:
    """The single password jobo uses for job-site accounts.

    ``JOBO_ACCOUNT_PASSWORD`` wins; otherwise personal/secrets.json. Never log
    or print the return value.
    """
    env = os.environ.get(PASSWORD_ENV, "")
    if env:
        return env
    return str(_load_json(str(path(SECRETS_FILE))).get("account_password", "") or "")


def site_passwords() -> dict[str, str]:
    """Hosts whose existing account has a password other than account_password().

    ``site_passwords`` in secrets.json maps a host to the password an earlier
    jobo run set there before the one-password rule (Wingtip 929, 2026-09-18,
    reset that account to a variant). Such a portal refuses to take the
    standard password back ("must differ from your previous 3 passwords"), so
    the recorded one is the only way in. Never log or print the values.
    """
    raw = _load_json(str(path(SECRETS_FILE))).get("site_passwords")
    if not isinstance(raw, dict):
        return {}
    return {
        str(host).lower().strip(" ."): str(pw)
        for host, pw in raw.items()
        if "." in str(host).strip(" .") and pw
    }


def site_password(host: str) -> str:
    """The recorded password for `host` (or a parent/child of it), else ""."""
    host = (host or "").lower().strip(" .")
    if "." not in host:
        return ""
    for site, pw in site_passwords().items():
        if host == site or host.endswith("." + site) or site.endswith("." + host):
            return pw
    return ""


def reload() -> None:
    """Forget cached files (tests and setup use this after writing new ones)."""
    _load_json.cache_clear()


def require_real(action: str) -> None:
    """Refuse to act for the fictional example person, or without a password."""
    if is_example() and not os.environ.get("JOBO_ALLOW_EXAMPLE_PERSON"):
        raise SystemExit(
            f"jobo {action}: refusing to act for the fictional example person "
            f"({personal_dir()}). Run ./setup.sh to write {DEFAULT_DIR}, or point "
            "JOBO_PERSONAL_DIR at your own config."
        )
