"""``jobo doctor``: is this machine ready to run jobo? One line per item.

Each check returns OK, MISSING, WARN or SKIP plus a fix hint. Exit 0 means
nothing is MISSING: WARN items are optional (cron, data backup), SKIP items
were not checked. With ``offline=True`` (``jobo doctor --offline``, used by
tests and fresh-clone checks) the items that need a real login - the Gmail
OAuth client and token, the LinkedIn session and the Hermes profile (cloned
from a signed-in Hermes) - report ``SKIP (needs login)`` instead of failing.

Never prints the account password, only whether one is set.

Everything that touches the machine (PATH lookups, ``crontab -l``, the Ollama
HTTP API) goes through the injectable ``Probe`` so the regression suite can
run doctor without spawning processes or opening sockets.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from policy import personal

REPO_ROOT = Path(__file__).resolve().parents[1]

OK, MISSING, WARN, SKIP = "OK", "MISSING", "WARN", "SKIP"
NEEDS_LOGIN = "SKIP (needs login)"

# Import names of the pyproject dependencies jobo uses at runtime.
PY_MODULES = (
    "click", "googleapiclient", "google_auth_oauthlib", "sqlalchemy", "bs4",
    "lxml", "dateutil", "dotenv", "playwright", "httpx", "pydantic",
    "websocket",
)
REQUIRED_FIELDS = ("full_name", "email", "phone", "location", "linkedin_url")
RESUME_SECTIONS = (r"\section{Programming Skills}", r"\section{Projects}")
CHROME_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
    "brave-browser",
)
DEFAULT_OLLAMA_MODEL = "gemma3:4b"  # module2/ollama_client.py
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
CRON_MARKER = "scripts/daily_pipeline.sh"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Probe:
    """Side-effecting lookups, replaceable in tests."""

    which: Callable[[str], str | None] = shutil.which
    crontab: Callable[[], str | None] = None  # type: ignore[assignment]
    ollama_tags: Callable[[str], list[str] | None] = None  # type: ignore[assignment]
    home: Callable[[], Path] = Path.home

    def __post_init__(self):
        if self.crontab is None:
            self.crontab = read_crontab
        if self.ollama_tags is None:
            self.ollama_tags = fetch_ollama_tags


def read_crontab() -> str | None:
    """Current user's crontab text ('' when none), None when crontab is absent."""
    if not shutil.which("crontab"):
        return None
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def fetch_ollama_tags(host: str) -> list[str] | None:
    """Model names the Ollama server has pulled, None when unreachable."""
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=3) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        return None
    return [m.get("name", "") for m in data.get("models", [])]


def settings(root: Path) -> dict[str, str]:
    """.env values with the process environment on top (as main.py loads them)."""
    env_file = root / ".env"
    values = {k: v or "" for k, v in dotenv_values(env_file).items()} if env_file.is_file() else {}
    values.update(os.environ)
    return values


def model_matches(model: str, tags: list[str]) -> bool:
    want = model if ":" in model else model + ":latest"
    return want in tags or model in tags


def resume_sections_missing(tex: Path) -> list[str]:
    text = tex.read_text(encoding="utf-8", errors="replace")
    return [s for s in RESUME_SECTIONS if s not in text]


def linkedin_signed_in(profile: Path) -> bool:
    """li_at cookie present in the profile (name only; values stay encrypted)."""
    db = profile / "Default" / "Cookies"
    if not db.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            row = con.execute(
                "SELECT count(*) FROM cookies WHERE name='li_at' AND host_key LIKE '%linkedin.com'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def _example_email() -> str:
    try:
        return json.loads((personal.EXAMPLE_DIR / personal.CANDIDATE_FILE).read_text())["email"]
    except (OSError, ValueError, KeyError):
        return ""


def run_checks(root: Path = REPO_ROOT, *, offline: bool = False, probe: Probe | None = None) -> list[Check]:
    probe = probe or Probe()
    cfg = settings(root)
    personal.reload()
    checks: list[Check] = []
    add = lambda *a: checks.append(Check(*a))

    missing_mods = [m for m in PY_MODULES if importlib.util.find_spec(m) is None]
    if missing_mods:
        add("python deps", MISSING, "not importable: " + ", ".join(missing_mods), "run: uv sync")
    else:
        add("python deps", OK, f"{len(PY_MODULES)} modules importable")

    pdir = personal.personal_dir()
    present = [n for n in (personal.CANDIDATE_FILE, personal.FACTS_FILE, personal.PROFILE_FILE,
                           personal.RESUME_TEX_FILE, personal.RESUME_PDF_FILE, personal.SECRETS_FILE)
               if (pdir / n).is_file()]
    if personal.is_example():
        add("personal config", MISSING, f"using the fictional example ({pdir.name}/)",
            "run ./setup.sh (writes personal/) or set JOBO_PERSONAL_DIR")
    elif len(present) < 6:
        add("personal config", MISSING, f"{pdir}: has {', '.join(present) or 'nothing'}",
            "run ./setup.sh to write the missing files")
    else:
        add("personal config", OK, f"{pdir}: {', '.join(present)}")

    cand = personal.candidate()
    empty = [k for k in REQUIRED_FIELDS if not str(cand.get(k) or "").strip()]
    if not cand:
        add("candidate.json", MISSING, "missing or not valid JSON", "run ./setup.sh")
    elif empty:
        add("candidate.json", MISSING, "empty: " + ", ".join(empty), "fill them in candidate.json or re-run ./setup.sh")
    elif cand.get("email") == _example_email() and not personal.is_example():
        add("candidate.json", WARN, f"still the fictional example person ({cand.get('email')})",
            "re-run ./setup.sh with your own details")
    else:
        chains = [k for k in ("school", "city") if personal.answer_chain(k)[1]]
        add("candidate.json", OK, f"{len(REQUIRED_FIELDS)} required fields set; answer chains: {', '.join(chains) or 'none'}")

    if not personal.candidate_facts():
        add("candidate_facts.md", MISSING, "missing or empty", "run ./setup.sh")
    else:
        add("candidate_facts.md", OK, f"{len(personal.candidate_facts().splitlines())} lines")

    secrets = personal.path(personal.SECRETS_FILE)
    if not personal.account_password():
        add("account password", MISSING, "not set", "run ./setup.sh (stored in personal/secrets.json, mode 600)")
    else:
        src = "env JOBO_ACCOUNT_PASSWORD" if os.environ.get(personal.PASSWORD_ENV) else "secrets.json"
        if secrets.is_file() and stat.S_IMODE(secrets.stat().st_mode) & 0o077:
            add("account password", WARN, f"set ({src}); secrets.json is readable by others", f"chmod 600 {secrets}")
        else:
            add("account password", OK, f"set ({src}, hidden)")

    tex, pdf = personal.resume_tex_path(), personal.resume_pdf_path()
    if not tex.is_file():
        add("resume.tex", MISSING, f"{tex} not found", "run ./setup.sh (own .tex or the bundled template)")
    elif lost := resume_sections_missing(tex):
        add("resume.tex", MISSING, "lacks " + " and ".join(lost), "module3 tailors only these sections; add them")
    else:
        add("resume.tex", OK, "has Programming Skills + Projects sections")
    if pdf.is_file():
        add("resume.pdf", OK, f"{pdf.stat().st_size} bytes")
    else:
        add("resume.pdf", MISSING, f"{pdf} not found", f"latexmk -pdf {tex.name} in {tex.parent}")

    try:
        from module3.context_loader import get_introduction_markdown, list_project_names
        md = personal.profile_path().read_text(encoding="utf-8")
        projects = list_project_names(md.split("# Projects", 1)[1]) if "# Projects" in md else []
        if not get_introduction_markdown(md):
            add("profile.md", MISSING, "no '# Introduction' section", "see personal.example/profile.md")
        elif not projects:
            add("profile.md", MISSING, "no '## <project>' under '# Projects'", "see personal.example/profile.md")
        else:
            add("profile.md", OK, f"intro + {len(projects)} project(s)")
    except OSError:
        add("profile.md", MISSING, f"{personal.profile_path()} not found", "run ./setup.sh")

    if (root / ".env").is_file():
        add(".env", OK, str(root / ".env"))
    else:
        add(".env", MISSING, "not found", "cp .env.example .env (./setup.sh does this)")

    host = cfg.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST
    model = cfg.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
    tags = probe.ollama_tags(host)
    if tags is None:
        add("ollama", MISSING, f"not reachable at {host}",
            "install: curl -fsSL https://ollama.com/install.sh | sh ; then: ollama serve")
    elif not model_matches(model, tags):
        add("ollama", MISSING, f"reachable, model {model} not pulled", f"ollama pull {model}")
    else:
        add("ollama", OK, f"{host}, model {model} pulled")

    backend = (cfg.get("JOBO_LLM_BACKEND") or "claude").strip()
    claude, hermes = probe.which("claude"), cfg.get("JOBO_HERMES_BIN") or probe.which("hermes")
    if hermes and not Path(hermes).exists():
        hermes = None
    if backend == "hermes" and not hermes:
        add("triage LLM", MISSING, "JOBO_LLM_BACKEND=hermes but hermes not found", "install Hermes")
    elif backend != "hermes" and not claude:
        add("triage LLM", MISSING, "claude CLI not on PATH", "curl -fsSL https://claude.ai/install.sh | bash ; then: claude (sign in)")
    else:
        add("triage LLM", OK, f"backend {backend}: {hermes if backend == 'hermes' else claude}")
    pandoc = probe.which("pandoc")
    if pandoc:
        add("pandoc (cover letters)", OK, pandoc)
    else:
        add("pandoc (cover letters)", WARN, "not on PATH: triage cannot write cover-letter PDFs",
            "install: sudo apt install pandoc (or https://pandoc.org/installing.html)")
    if hermes:
        add("hermes (apply agent)", OK, hermes)
    else:
        add("hermes (apply agent)", MISSING, "hermes not on PATH", "install Hermes, sign in, then re-run ./setup.sh")

    prof = probe.home() / ".hermes" / "profiles" / (cfg.get("JOBO_HERMES_PROFILE") or "jobo")
    if prof.is_dir():
        add("hermes jobo profile", OK, str(prof))
    elif offline:
        add("hermes jobo profile", NEEDS_LOGIN, f"{prof} not found (cloned from a signed-in Hermes)",
            "after signing in to Hermes: bash scripts/setup_hermes_profile.sh && bash scripts/pin_hermes.sh")
    else:
        add("hermes jobo profile", MISSING, f"{prof} not found",
            "bash scripts/setup_hermes_profile.sh && bash scripts/pin_hermes.sh")

    chrome = cfg.get("JOBO_CHROME_BIN") or ""
    chrome = chrome if chrome and Path(chrome).exists() else next(
        (p for p in map(probe.which, CHROME_CANDIDATES) if p), None)
    if chrome:
        add("chromium", OK, chrome)
    else:
        add("chromium", MISSING, "no Chromium-family browser", "sudo apt install chromium (or set JOBO_CHROME_BIN)")

    raw_profile = (cfg.get("JOBO_BROWSER_PROFILE") or "").strip()
    bprof = Path(raw_profile).expanduser() if raw_profile else probe.home() / "jobo-linkedin-profile"
    if bprof.is_dir():
        add("browser profile", OK, str(bprof))
    else:
        add("browser profile", MISSING, f"{bprof} not found", "run ./setup.sh (browser step)")
    if offline:
        add("linkedin session", NEEDS_LOGIN, "not checked in offline mode",
            "./setup.sh browser step: sign in to LinkedIn in jobo's browser")
    elif linkedin_signed_in(bprof):
        add("linkedin session", OK, "li_at cookie present")
    else:
        add("linkedin session", MISSING, "not signed in", "./setup.sh browser step: sign in to LinkedIn in jobo's browser")

    for label, rel, hint in (
        ("gmail client", "credentials/client_secret.json",
         "create a Desktop OAuth client in your own Google Cloud project (./setup.sh walks you through it)"),
        ("gmail token", "credentials/token.json", "uv run python auth_manual.py"),
    ):
        if (root / rel).is_file():
            add(label, OK, rel)
        elif offline:
            add(label, NEEDS_LOGIN, f"{rel} not found", hint)
        else:
            add(label, MISSING, f"{rel} not found", hint)

    tools = [t for t in ("latexmk", "pdflatex") if probe.which(t)]
    if tools:
        add("latex", OK, ", ".join(tools))
    else:
        add("latex", MISSING, "latexmk/pdflatex not found (needed for tailored resumes)",
            "sudo apt install latexmk texlive-latex-extra texlive-fonts-recommended")

    from module4 import budget_guard
    limits = []
    for key, default in (("JOBO_SESSION_THRESHOLD", budget_guard.DEFAULT_SESSION_THRESHOLD),
                         ("JOBO_BUDGET_THRESHOLD", budget_guard.DEFAULT_THRESHOLD)):
        raw = (cfg.get(key) or "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError:
            value = None
        limits.append((key, raw, value, default))
    bad = [k for k, _, v, _ in limits if v is None or not 0 < v <= 1]
    detail = "; ".join(f"{'session' if 'SESSION' in k else 'weekly'} {v * 100:.0f}%"
                       + ("" if raw else " (default)") for k, raw, v, _ in limits if v is not None)
    if bad:
        add("usage limits", WARN, "invalid: " + ", ".join(bad), "set a fraction 0-1 in .env, e.g. 0.30")
    else:
        add("usage limits", OK, detail)

    tab = probe.crontab()
    if tab is None:
        add("cron", WARN, "crontab not installed", "install cron, then re-run ./setup.sh")
    elif any(CRON_MARKER in line and str(root) in line and not line.lstrip().startswith("#")
             for line in tab.splitlines()):
        add("cron", OK, "daily_pipeline.sh scheduled")
    else:
        add("cron", WARN, "no twice-daily run installed (optional)", "re-run ./setup.sh and answer yes to cron")

    repo = (cfg.get("JOBO_DATA_REPO") or "").strip()
    if repo:
        add("data backup repo", OK, f"JOBO_DATA_REPO={repo}")
    else:
        add("data backup repo", WARN, "JOBO_DATA_REPO not set (optional)", "re-run ./setup.sh, or see scripts/backup_data.sh")
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == MISSING for c in checks) else 0


def render(checks: list[Check], *, offline: bool = False) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        line = f"{c.status:<18} {c.name:<{width}}  {c.detail}"
        if c.hint and c.status != OK:
            line += f"\n{'':<18} {'':<{width}}  fix: {c.hint}"
        lines.append(line)
    counts = {s: sum(c.status.startswith(s) for c in checks) for s in (OK, MISSING, WARN, SKIP)}
    lines.append("")
    lines.append(", ".join(f"{n} {s}" for s, n in counts.items()))
    if offline:
        lines.append("offline mode: items that need a real login were skipped, not checked")
    lines.append("READY" if not counts[MISSING] else "NOT READY: fix the MISSING items above")
    return "\n".join(lines)


def to_json(checks: list[Check], *, offline: bool = False) -> str:
    return json.dumps({"offline": offline, "ok": exit_code(checks) == 0,
                       "checks": [asdict(c) for c in checks]}, indent=2)
