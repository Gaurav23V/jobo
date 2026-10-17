"""First-run setup behind ``./setup.sh``: tools, your details, resume, logins.

Interactive by default. Safe to re-run: every question shows the current value
as its default (from ``personal/setup_answers.json``, which each run saves
without the password), and no existing file is replaced without a yes.

    ./setup.sh                                   interactive
    ./setup.sh --non-interactive --answers FILE  no prompts (JSON, see
                                                 setup.answers.example.json)
    ./setup.sh --skip-installs                   only check system software

Non-interactive mode never does what needs a real login or touches accounts:
the Gmail sign-in, the LinkedIn sign-in in jobo's browser and ``gh repo
create`` are skipped and listed at the end; the cron lines are installed only
with ``"install_cron": true``. Existing files that would change are kept
unless ``"overwrite": true``.

Machine access (PATH lookups, crontab, Ollama, subprocesses, prompts) is
injected so scripts/test_setup_wizard.py can run the whole wizard offline.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from jobo_setup import doctor
from policy import personal

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = personal.EXAMPLE_DIR
SAVED_ANSWERS = "setup_answers.json"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"  # .env.example: small model is enough
CRON_TIMES = ("0 10", "0 22")
SECRET_KEYS = ("account_password",)

GMAIL_STEPS = """\
Gmail access uses your OWN Google Cloud OAuth client (nothing is shared):
  1. https://console.cloud.google.com/ -> create a project (any name).
  2. APIs & Services -> Library -> enable "Gmail API".
  3. APIs & Services -> OAuth consent screen -> External; add your Gmail
     address under Test users (or publish the app).
  4. Scopes: add .../auth/gmail.modify (jobo reads job-alert mail and marks
     it read; it never sends or deletes).
  5. Credentials -> Create credentials -> OAuth client ID -> Desktop app ->
     Download JSON.
"""


def _get(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


def read_env_file(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def env_value(path: Path, key: str) -> str:
    value = ""
    for line in read_env_file(path):
        if line.startswith(key + "="):
            value = line.split("=", 1)[1].strip().strip("'\"")
    return value


def set_env_key(path: Path, key: str, value: str) -> bool:
    """Set KEY=value in .env, keeping every other line. True when it changed."""
    lines = read_env_file(path)
    new = f"{key}={value}"
    hits = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
    if hits and all(lines[i] == new for i in hits):
        return False
    if hits:
        lines[hits[-1]] = new
        lines = [line for i, line in enumerate(lines) if i not in hits[:-1]]
    else:
        lines.append(new)
    _write_private(path, "\n".join(lines) + "\n")
    return True


def _write_private(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8") if isinstance(text, str) else text)
    os.chmod(path, 0o600)


def cron_lines(root: Path) -> list[str]:
    script = shlex.quote(str(root / "scripts" / "daily_pipeline.sh"))
    log = shlex.quote(str(root / "data" / "logs" / "cron.log"))
    return [f"{t} * * * {script} >> {log} 2>&1" for t in CRON_TIMES]


def merge_crontab(current: str, wanted: list[str]) -> str | None:
    """New crontab text with the missing lines appended, None if nothing to add."""
    have = {line.strip() for line in current.splitlines()}
    missing = [w for w in wanted if w not in have]
    if not missing:
        return None
    text = current if not current or current.endswith("\n") else current + "\n"
    return text + "\n".join(missing) + "\n"


def chain(name: str, spellings: list[str]) -> dict[str, list[str]]:
    options = [s.strip() for s in [*spellings, name] if s and s.strip()]
    options = list(dict.fromkeys(options))
    return {"triggers": list(dict.fromkeys(o.lower() for o in options)), "options": options}


def build_candidate(a: dict) -> dict:
    name = a.get("full_name", "").strip()
    out = {
        "full_name": name,
        "first_name": a.get("first_name") or (name.split()[0] if name else ""),
        "email": a.get("email", ""),
        "phone": a.get("phone", ""),
        "linkedin_url": a.get("linkedin_url", ""),
        "github_url": a.get("github_url", ""),
        "website_url": a.get("website_url", ""),
        "location": a.get("location", ""),
        "upload_slug": a.get("upload_slug") or "_".join(
            "".join(c if c.isalnum() else " " for c in name).split()),
        "answer_chains": {},
    }
    school, city = a.get("school") or {}, a.get("city") or {}
    if school.get("name") or school.get("spellings"):
        out["answer_chains"]["school"] = chain(school.get("name", ""), school.get("spellings") or [])
    if city.get("name") or city.get("spellings"):
        out["answer_chains"]["city"] = chain(city.get("name", ""), city.get("spellings") or [])
    return out


def build_facts(a: dict) -> str:
    job = a.get("current_job") or {}
    edu = a.get("education") or {}
    school = (a.get("school") or {}).get("name", "")
    years = str(a.get("years_experience", "")).strip()
    lines = [
        "Candidate facts (single source of truth - never contradict these):",
        f"- Name: {a.get('full_name', '')}. Email: {a.get('email', '')}. Phone: {a.get('phone', '')}.",
        f"- LinkedIn: {a.get('linkedin_url', '')}",
        f"  GitHub: {a.get('github_url', '')}",
        f"  Website: {a.get('website_url', '')}",
        "  Use these exact profile URLs; never construct a URL from the candidate's name.",
    ]
    if job.get("employer"):
        lines += [
            f"- Current employer: {job['employer']}, {job.get('title', '')}, {job.get('location', '')} "
            f"({job.get('start', '')} - present, {job.get('employment_type') or 'full-time'}). "
            "Fill current company / current title / current employer fields with these.",
            f"  \"May we contact your current employer?\": answer {a.get('contact_current_employer') or 'No'}.",
        ]
    else:
        lines.append("- Current employer: none (not currently employed).")
    lines.append(f"- Location: {a.get('location', '')} (current). {a.get('relocation', '')}".rstrip())
    if school or edu:
        gpa = f" GPA: {edu['gpa']} (fill this exact value whenever a form asks for GPA)." if edu.get("gpa") else ""
        lines.append(f"- Education: {edu.get('degree', '')}, {school}, {edu.get('location', '')}, "
                     f"{edu.get('start', '')} - {edu.get('end', '')}.{gpa}")
    lines.append(f"- Experience: ~{years} years total:")
    jobs = ([{**job, "end": "present"}] if job.get("employer") else []) + list(a.get("work_history") or [])
    for j in jobs:
        summary = f": {j['summary']}" if j.get("summary") else ""
        lines.append(f"  * {j.get('employer', '')}, {j.get('title', '')}, {j.get('location', '')} "
                     f"({j.get('start', '')} - {j.get('end', '')}){summary}")
    if a.get("current_pay"):
        lines.append(f"- Current salary: {a['current_pay']}.")
    if a.get("expected_pay"):
        rng = f" If a range is demanded, {a['expected_pay_range']}." if a.get("expected_pay_range") else ""
        lines.append(f"- Expected salary: {a['expected_pay']}. Fill ONLY on forms that explicitly ask for "
                     f"expected salary.{rng}")
    lines += [
        f"- Available to join: {a.get('notice_period', '')}.",
        f"- Work authorization: {a.get('work_authorization', '')}.",
        f"- Years of experience: write \"{years}\" when the field asks numerically; for a range "
        f"picker choose the band containing {years}.",
    ]
    return "\n".join(lines) + "\n"


def build_profile(a: dict) -> str:
    job = a.get("current_job") or {}
    edu = a.get("education") or {}
    school = (a.get("school") or {}).get("name", "")
    out = [
        "<!--",
        "Written by ./setup.sh as a starting point: edit it freely (re-running setup",
        "asks before replacing it). Parser rule (module3/context_loader.py): every",
        "`## Heading` under `# Projects` is one project. Use `#` for other sections.",
        "-->",
        "",
        "# Introduction",
        "",
        f"Hi, I'm {a.get('full_name', '')}, {a.get('headline') or 'a software engineer'} in "
        f"{a.get('location', '')}."
        + (f" I studied {edu.get('degree', '')} at {school} ({edu.get('start', '')} - {edu.get('end', '')})."
           if school else ""),
        "",
        "**What I want next**",
        "",
        a.get("job_preferences") or "Describe the roles you want and the ones to skip.",
        "",
        "# Experience",
        "",
        f"I have about {a.get('years_experience', '')} years of professional experience.",
        "",
    ]
    jobs = ([{**job, "end": "present"}] if job.get("employer") else []) + list(a.get("work_history") or [])
    for j in jobs:
        out += [f"## {j.get('employer', '')} - {j.get('title', '')} ({j.get('start', '')} - "
                f"{j.get('end', '')}, {j.get('location', '')})", f"- {j.get('summary') or 'What you did.'}", ""]
    out += ["# Skills", ""]
    out += [f"- {s}" for s in (a.get("skills") or ["List your skills"])]
    out += ["", "# Projects", ""]
    projects = a.get("projects") or [{"name": "Example Project", "description": "Replace with a real project."}]
    for p in projects:
        out += [f"## {p.get('name', 'Project')}"]
        if p.get("url"):
            out.append(f"- **GitHub:** {p['url']}")
        if p.get("stack"):
            out.append(f"- **Stack:** {p['stack']}")
        out += [f"- **Description:** {p.get('description', '')}", ""]
    return "\n".join(out).rstrip() + "\n"


class Wizard:
    def __init__(
        self,
        root: Path = REPO_ROOT,
        *,
        answers: dict | None = None,
        interactive: bool = True,
        skip_installs: bool = False,
        personal_dir: Path | None = None,
        probe: doctor.Probe | None = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        input_fn: Callable[[str], str] = input,
        getpass_fn: Callable[[str], str] = getpass.getpass,
        out: Callable[[str], None] = print,
    ):
        self.root = Path(root)
        self.interactive = interactive
        self.skip_installs = skip_installs
        self.probe = probe or doctor.Probe()
        self.run = run
        self.input = input_fn
        self.getpass = getpass_fn
        self.out = out
        self.pdir = Path(personal_dir) if personal_dir else self._default_personal_dir()
        self.env_path = self.root / ".env"
        saved = self.pdir / SAVED_ANSWERS
        self.saved: dict = json.loads(saved.read_text()) if saved.is_file() else {}
        # Answer file first, then what the last run saved.
        self.answers: dict = {**self.saved, **(answers or {})}
        self.report: list[tuple[str, str, str]] = []

    def _default_personal_dir(self) -> Path:
        raw = os.environ.get("JOBO_PERSONAL_DIR", "").strip()
        if raw and self.root == REPO_ROOT:
            p = Path(raw).expanduser()
            p = p if p.is_absolute() else self.root / p
            if p.resolve() != TEMPLATE_DIR.resolve():
                return p
        return self.root / "personal"

    # ---- prompts -----------------------------------------------------------

    def note(self, step: str, status: str, detail: str = "") -> None:
        self.report.append((step, status, detail))
        self.out(f"  [{status}] {step}: {detail}" if detail else f"  [{status}] {step}")

    def ask(self, key: str, question: str, default: Any = "") -> str:
        current = _get(self.answers, key, default)
        current = "" if current is None else str(current)
        if not self.interactive:
            return current
        reply = self.input(f"{question} [{current}]: " if current else f"{question}: ").strip()
        value = reply or current
        _set(self.answers, key, value)
        return value

    def ask_list(self, key: str, question: str) -> list[str]:
        current = _get(self.answers, key, []) or []
        if not self.interactive:
            return list(current)
        shown = "; ".join(current)
        reply = self.input(f"{question} (separate with ;) [{shown}]: ").strip()
        value = [s.strip() for s in reply.split(";") if s.strip()] if reply else list(current)
        _set(self.answers, key, value)
        return value

    def yes(self, key: str, question: str, default: bool = False) -> bool:
        value = _get(self.answers, key, default)
        if not self.interactive:
            return bool(value)
        hint = "Y/n" if value else "y/N"
        reply = self.input(f"{question} [{hint}]: ").strip().lower()
        return bool(value) if not reply else reply in {"y", "yes"}

    def confirm(self, question: str, default: bool = False) -> bool:
        """A yes/no that is never answered from the file (overwrite questions)."""
        if not self.interactive:
            return bool(self.answers.get("overwrite", False))
        hint = "Y/n" if default else "y/N"
        reply = self.input(f"{question} [{hint}]: ").strip().lower()
        return default if not reply else reply in {"y", "yes"}

    def write(self, step: str, path: Path, content: str | bytes, *, private: bool = False) -> bool:
        data = content.encode("utf-8") if isinstance(content, str) else content
        rel = self._rel(path)
        if path.is_file():
            if path.read_bytes() == data:
                self.note(step, "UNCHANGED", rel)
                return True
            if not self.confirm(f"{rel} exists and would change. Overwrite?"):
                self.note(step, "KEPT", f"{rel} exists and differs; kept yours")
                return False
        if private:
            _write_private(path, data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.note(step, "WROTE", rel)
        return True

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def sh(self, argv: list[str], **kw) -> bool:
        self.out("  $ " + " ".join(shlex.quote(a) for a in argv))
        try:
            return self.run(argv, cwd=str(kw.pop("cwd", self.root)), **kw).returncode == 0
        except OSError as exc:
            self.out(f"    failed: {exc}")
            return False

    # ---- steps -------------------------------------------------------------

    def step_env(self) -> None:
        self.out("\n== .env and usage limits ==")
        if not self.env_path.is_file():
            example = self.root / ".env.example"
            _write_private(self.env_path, example.read_text() if example.is_file() else "")
            self.note(".env", "WROTE", "from .env.example")
        else:
            self.note(".env", "UNCHANGED", "exists")
        model = self.ask("ollama_model", "Ollama model for enrichment",
                         env_value(self.env_path, "OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL)
        from module4 import budget_guard
        session = self.ask("usage_limits.session",
                           "Stop runs at this share of the 5-hour Claude session (0-1)",
                           env_value(self.env_path, "JOBO_SESSION_THRESHOLD")
                           or f"{budget_guard.DEFAULT_SESSION_THRESHOLD:.2f}")
        weekly = self.ask("usage_limits.weekly",
                          "Stop runs at this share of the weekly Claude allowance (0-1)",
                          env_value(self.env_path, "JOBO_BUDGET_THRESHOLD")
                          or f"{budget_guard.DEFAULT_THRESHOLD:.2f}")
        for key, value in (("OLLAMA_MODEL", model), ("JOBO_SESSION_THRESHOLD", session),
                           ("JOBO_BUDGET_THRESHOLD", weekly)):
            if key != "OLLAMA_MODEL":
                try:
                    if not 0 < float(value) <= 1:
                        raise ValueError
                except ValueError:
                    self.note(key, "FAIL", f"{value!r} is not a fraction between 0 and 1")
                    continue
            changed = set_env_key(self.env_path, key, value)
            self.note(key, "SET" if changed else "UNCHANGED", value)

    def step_tools(self) -> None:
        self.out("\n== Software ==")
        which = self.probe.which
        self.note("uv", "OK" if which("uv") else "TODO",
                  which("uv") or "install: curl -LsSf https://astral.sh/uv/install.sh | sh")
        chrome = next((p for p in map(which, doctor.CHROME_CANDIDATES) if p), None)
        if chrome:
            self.note("chromium", "OK", chrome)
        elif self.skip_installs:
            self.note("chromium", "TODO", "sudo apt install chromium   (or: uv run playwright install chromium)")
        elif self.yes("install_playwright_chromium", "No Chromium found. Install Playwright's Chromium?", True):
            if self.sh([sys.executable, "-m", "playwright", "install", "chromium"]):
                found = sorted(Path(self.probe.home(), ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
                if found:
                    set_env_key(self.env_path, "JOBO_CHROME_BIN", str(found[-1]))
                    self.note("chromium", "DONE", f"JOBO_CHROME_BIN={found[-1]}")
                else:
                    self.note("chromium", "FAIL", "installed, but chrome binary not found")
            else:
                self.note("chromium", "FAIL", "playwright install chromium failed")
        else:
            self.note("chromium", "TODO", "sudo apt install chromium")

        model = env_value(self.env_path, "OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
        host = env_value(self.env_path, "OLLAMA_HOST") or doctor.DEFAULT_OLLAMA_HOST
        tags = self.probe.ollama_tags(host)
        if not which("ollama") and tags is None:
            self.note("ollama", "TODO", "install: curl -fsSL https://ollama.com/install.sh | sh")
        elif tags is None:
            self.note("ollama", "TODO", f"not running at {host}: ollama serve")
        elif doctor.model_matches(model, tags):
            self.note("ollama", "OK", f"model {model} pulled")
        elif self.skip_installs or not self.yes("pull_ollama_model", f"Pull Ollama model {model} now?", True):
            self.note("ollama", "TODO", f"ollama pull {model}")
        else:
            self.note("ollama", "DONE" if self.sh(["ollama", "pull", model]) else "FAIL", f"ollama pull {model}")

        claude = which("claude")
        self.note("claude CLI", "OK" if claude else "TODO",
                  claude or "install: curl -fsSL https://claude.ai/install.sh | bash ; then run `claude` once to sign in")
        hermes = which("hermes")
        if not hermes:
            self.note("hermes", "TODO", "install Hermes Agent and sign it in, then re-run ./setup.sh")
        else:
            self.note("hermes", "OK", hermes)
            scripts = self.root / "scripts"
            if self.yes("setup_hermes_profile", "Create/refresh the Hermes 'jobo' profile (scripts/setup_hermes_profile.sh)?"):
                ok = self.sh(["bash", str(scripts / "setup_hermes_profile.sh")])
                self.note("hermes jobo profile", "DONE" if ok else "FAIL", "setup_hermes_profile.sh")
            else:
                self.note("hermes jobo profile", "SKIPPED", "run: bash scripts/setup_hermes_profile.sh")
            if self.yes("pin_hermes", "Pin the Hermes version the apply agent uses (scripts/pin_hermes.sh)?"):
                ok = self.sh(["bash", str(scripts / "pin_hermes.sh")])
                self.note("hermes pin", "DONE" if ok else "FAIL", "pin_hermes.sh")
            else:
                self.note("hermes pin", "SKIPPED", "run: bash scripts/pin_hermes.sh")
        tex = [t for t in ("latexmk", "pdflatex") if which(t)]
        self.note("latex", "OK" if tex else "TODO", ", ".join(tex) or
                  "sudo apt install latexmk texlive-latex-extra texlive-fonts-recommended")

    def step_person(self) -> None:
        self.out("\n== About you (written to personal/, never committed) ==")
        for key, q in (("full_name", "Full name"), ("first_name", "First name (blank = first word of name)"),
                       ("email", "Email"), ("phone", "Phone (with country code)"),
                       ("location", "Current location (City, Country)"),
                       ("linkedin_url", "LinkedIn profile URL"), ("github_url", "GitHub URL (optional)"),
                       ("website_url", "Website (optional)"),
                       ("headline", "One-line headline, e.g. 'a backend engineer'")):
            self.ask(key, q)
        self.ask("school.name", "School / university (official name)")
        self.ask_list("school.spellings", "Other spellings of the school forms may list, most specific first")
        self.ask("city.name", "Your city as forms usually spell it")
        self.ask_list("city.spellings", "Other spellings of your city, most specific first")
        for key, q in (("education.degree", "Degree, e.g. B.S. Computer Science"),
                       ("education.location", "School location"), ("education.start", "Started (e.g. Aug 2019)"),
                       ("education.end", "Finished (e.g. May 2023)"), ("education.gpa", "GPA (optional)")):
            self.ask(key, q)
        for key, q in (("current_job.employer", "Current employer (blank if none)"),
                       ("current_job.title", "Current job title"), ("current_job.location", "Job location"),
                       ("current_job.start", "Started (e.g. Jun 2023)"),
                       ("current_job.employment_type", "Employment type (full-time, contract, ...)"),
                       ("current_job.summary", "One line on what you do there")):
            self.ask(key, q)
        if self.interactive:
            history = list(self.answers.get("work_history") or [])
            self.out(f"Past jobs on file: {len(history)}")
            if history and self.confirm("Re-enter your past jobs?"):
                history = []
            while self.confirm("Add a past job?"):
                history.append({k: self.input(f"  {k}: ").strip()
                                for k in ("employer", "title", "location", "start", "end", "summary")})
            self.answers["work_history"] = history
        for key, q in (("years_experience", "Years of professional experience (number)"),
                       ("current_pay", "Current pay, e.g. '100,000 USD (base)' (optional)"),
                       ("expected_pay", "Expected pay"), ("expected_pay_range", "Expected range if a form demands one"),
                       ("notice_period", "Notice period, e.g. '30 days notice'"),
                       ("work_authorization", "Work authorisation, e.g. 'authorized to work in the USA, no sponsorship needed'"),
                       ("relocation", "Relocation / remote preference"),
                       ("job_preferences", "Job preferences: what you want next, and what to skip")):
            self.ask(key, q)
        self.ask_list("skills", "Skills lines for profile.md")
        a = self.answers
        missing = [k for k in doctor.REQUIRED_FIELDS if not str(a.get(k) or "").strip()]
        if missing:
            self.note("personal details", "FAIL", "missing: " + ", ".join(missing))
            return
        self.pdir.mkdir(parents=True, exist_ok=True)
        self.write("candidate.json", self.pdir / personal.CANDIDATE_FILE,
                   json.dumps(build_candidate(a), indent=2) + "\n")
        self.write("candidate_facts.md", self.pdir / personal.FACTS_FILE, build_facts(a))
        self.write("profile.md", self.pdir / personal.PROFILE_FILE, build_profile(a))

    def step_resume(self) -> None:
        self.out("\n== Resume ==")
        dest = self.pdir / personal.RESUME_TEX_FILE
        src_raw = self.ask("resume_tex", "Path to your LaTeX resume (blank = start from the bundled template)")
        src = Path(src_raw).expanduser() if src_raw else TEMPLATE_DIR / personal.RESUME_TEX_FILE
        if not src.is_file():
            self.note("resume.tex", "FAIL", f"{src} not found")
            return
        lost = doctor.resume_sections_missing(src)
        if lost:
            self.note("resume.tex", "FAIL", f"{src} lacks {' and '.join(lost)} (module3 tailors only those)")
            return
        self.pdir.mkdir(parents=True, exist_ok=True)
        if not self.write("resume.tex", dest, src.read_bytes()):
            return
        if not src_raw:
            self.out(f"    Template copied: edit {self._rel(dest)} with your details, then re-run ./setup.sh")
        pdf = self.pdir / personal.RESUME_PDF_FILE
        if pdf.is_file() and pdf.stat().st_mtime >= dest.stat().st_mtime:
            self.note("resume.pdf", "UNCHANGED", "newer than resume.tex")
            return
        if self.probe.which("latexmk"):
            with tempfile.TemporaryDirectory() as tmp:
                ok = self.sh(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
                              f"-outdir={tmp}", str(dest)], cwd=src.parent if src_raw else self.pdir,
                             capture_output=True)
                built = Path(tmp) / "resume.pdf"
                if ok and built.is_file():
                    self.write("resume.pdf", pdf, built.read_bytes())
                else:
                    self.note("resume.pdf", "FAIL", f"latexmk failed; run it by hand in {self.pdir}")
        elif not src_raw:
            self.write("resume.pdf", pdf, (TEMPLATE_DIR / personal.RESUME_PDF_FILE).read_bytes())
        else:
            self.note("resume.pdf", "TODO", "install latexmk and re-run, or put resume.pdf in personal/")

    def step_password(self) -> None:
        self.out("\n== Job-site account password ==")
        self.out("    jobo uses ONE password for every job-site account it creates or resets.")
        path = self.pdir / personal.SECRETS_FILE
        existing = ""
        if path.is_file():
            try:
                existing = json.loads(path.read_text()).get("account_password", "")
            except ValueError:
                existing = ""
        password = ""
        if self.interactive:
            if existing and self.confirm("A password is already saved. Keep it?", True):
                self.note("password", "KEPT", "personal/secrets.json")
                return
            while True:
                password = self.getpass("Password (hidden): ")
                if password and password == self.getpass("Again: "):
                    break
                self.out("    Empty or did not match; try again.")
        else:
            env_name = self.answers.get("account_password_env") or ""
            password = os.environ.get(env_name, "") if env_name else str(self.answers.get("account_password") or "")
        if not password:
            self.note("password", "KEPT" if existing else "TODO",
                      "personal/secrets.json" if existing else "no password given")
            return
        self.write("password", path, json.dumps({"account_password": password}, indent=2) + "\n", private=True)

    def step_gmail(self) -> None:
        self.out("\n== Gmail ==")
        secret = self.root / "credentials" / "client_secret.json"
        src_raw = ""
        if self.interactive:
            self.out(GMAIL_STEPS)
        if secret.is_file() and not self.interactive:
            self.note("gmail client", "UNCHANGED", "credentials/client_secret.json")
        else:
            prompt = "Path to the downloaded client JSON" + (" (blank = keep current)" if secret.is_file() else "")
            src_raw = self.ask("gmail_client_secret", prompt)
            if not src_raw:
                self.note("gmail client", "UNCHANGED" if secret.is_file() else "TODO",
                          "credentials/client_secret.json" if secret.is_file() else "no client JSON given")
            else:
                src = Path(src_raw).expanduser()
                try:
                    ok = bool(json.loads(src.read_text())["installed"]["client_id"])
                except (OSError, ValueError, KeyError, TypeError):
                    ok = False
                if ok:
                    self.write("gmail client", secret, src.read_bytes(), private=True)
                else:
                    self.note("gmail client", "FAIL", f"{src} is not a Desktop-app OAuth client JSON")
        token = self.root / "credentials" / "token.json"
        if not self.interactive:
            self.note("gmail sign-in", "SKIPPED", "needs a real login: uv run python auth_manual.py")
        elif not secret.is_file():
            self.note("gmail sign-in", "TODO", "add the client JSON first")
        elif self.confirm("Sign in to Gmail now (opens your browser)?", not token.is_file()):
            ok = self.sh([sys.executable, str(REPO_ROOT / "auth_manual.py")])
            self.note("gmail sign-in", "DONE" if ok and token.is_file() else "FAIL", "credentials/token.json")
        else:
            self.note("gmail sign-in", "SKIPPED" if not token.is_file() else "UNCHANGED", "credentials/token.json")

    def step_browser(self) -> None:
        self.out("\n== jobo's browser (LinkedIn sign-in) ==")
        raw = env_value(self.env_path, "JOBO_BROWSER_PROFILE") or os.environ.get("JOBO_BROWSER_PROFILE", "")
        profile = Path(raw).expanduser() if raw else self.probe.home() / "jobo-linkedin-profile"
        if profile.is_dir():
            self.note("browser profile", "UNCHANGED", str(profile))
        else:
            profile.mkdir(parents=True, mode=0o700)
            self.note("browser profile", "WROTE", str(profile))
        if not self.interactive:
            self.note("linkedin sign-in", "SKIPPED", "needs a real login: re-run ./setup.sh interactively")
            return
        if not self.confirm("Open jobo's browser now to sign in to LinkedIn (and Google, optional)?", True):
            self.note("linkedin sign-in", "SKIPPED", "re-run ./setup.sh to sign in later")
            return
        try:
            from module4 import browser_session
            url = browser_session.ensure_browser_running(profile)
            import urllib.request
            req = urllib.request.Request(url + "/json/new?https://www.linkedin.com/login", method="PUT")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as exc:  # no DISPLAY, no browser, port busy...
            self.note("linkedin sign-in", "FAIL", f"could not open the browser: {exc}")
            return
        self.input("Sign in to LinkedIn in that window, then press Enter here (leave the browser open)...")
        self.note("linkedin sign-in", "DONE", "checked by jobo doctor")

    def step_cron(self) -> None:
        self.out("\n== Twice-daily runs (cron) ==")
        wanted = cron_lines(self.root)
        if not self.yes("install_cron", "Run jobo at 10:00 and 22:00 every day (adds 2 crontab lines)?"):
            self.note("cron", "SKIPPED", "not requested")
            return
        current = self.probe.crontab()
        if current is None:
            self.note("cron", "FAIL", "crontab not found")
            return
        text = merge_crontab(current, wanted)
        if text is None:
            self.note("cron", "UNCHANGED", "already installed")
            return
        (self.root / "data" / "logs").mkdir(parents=True, exist_ok=True)
        ok = self.sh(["crontab", "-"], input=text, text=True)
        self.note("cron", "DONE" if ok else "FAIL", "10:00 and 22:00 daily_pipeline.sh")

    def step_data_repo(self) -> None:
        self.out("\n== Private backup of your job history (optional) ==")
        repo = self.ask("data_repo", "Private GitHub repo for data/jobs.db backups, owner/name (blank = skip)",
                        env_value(self.env_path, "JOBO_DATA_REPO"))
        if not repo:
            self.note("data repo", "SKIPPED", "not configured")
            return
        changed = set_env_key(self.env_path, "JOBO_DATA_REPO", repo)
        self.note("JOBO_DATA_REPO", "SET" if changed else "UNCHANGED", repo)
        if not self.interactive:
            self.note("gh repo create", "SKIPPED", f"needs a real login: gh repo create {repo} --private")
        elif self.probe.which("gh") and self.confirm(f"Create {repo} now with `gh repo create --private`?"):
            ok = self.sh(["gh", "repo", "create", repo, "--private"])
            self.note("gh repo create", "DONE" if ok else "FAIL", repo)
        else:
            self.note("gh repo create", "SKIPPED", f"gh repo create {repo} --private")

    def save_answers(self) -> None:
        if not self.pdir.is_dir():
            return
        clean = {k: v for k, v in self.answers.items() if k not in SECRET_KEYS and k != "overwrite"}
        path = self.pdir / SAVED_ANSWERS
        text = json.dumps(clean, indent=2) + "\n"
        if not path.is_file() or path.read_text() != text:
            _write_private(path, text)

    def run_all(self) -> int:
        for step in (self.step_env, self.step_tools, self.step_person, self.step_resume,
                     self.step_password, self.step_gmail, self.step_browser, self.step_cron,
                     self.step_data_repo):
            step()
        self.save_answers()
        self.out("\n== Summary ==")
        for status in ("FAIL", "TODO", "SKIPPED"):
            rows = [r for r in self.report if r[1] == status]
            if rows:
                self.out(f"{status}:")
                for name, _, detail in rows:
                    self.out(f"  - {name}: {detail}")
        offline = not self.interactive
        self.out("\n== jobo doctor" + (" --offline" if offline else "") + " ==")
        os.environ["JOBO_PERSONAL_DIR"] = str(self.pdir)
        checks = doctor.run_checks(self.root, offline=offline, probe=self.probe)
        self.out(doctor.render(checks, offline=offline))
        failed = any(r[1] == "FAIL" for r in self.report)
        return 1 if failed else doctor.exit_code(checks)


def load_answers(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: answers must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="setup.sh", description="Set up jobo on this machine.")
    ap.add_argument("--non-interactive", action="store_true", help="no prompts; needs --answers")
    ap.add_argument("--answers", type=Path, help="JSON answers file (see setup.answers.example.json)")
    ap.add_argument("--skip-installs", action="store_true", help="only check system software, install nothing")
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--personal-dir", type=Path, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.non_interactive and not args.answers:
        ap.error("--non-interactive needs --answers FILE")
    answers = load_answers(args.answers) if args.answers else {}
    wizard = Wizard(args.root.resolve(), answers=answers, interactive=not args.non_interactive,
                    skip_installs=args.skip_installs, personal_dir=args.personal_dir)
    try:
        return wizard.run_all()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup interrupted; re-run ./setup.sh to continue (answers so far are kept).")
        wizard.save_answers()
        return 130


if __name__ == "__main__":
    sys.exit(main())
