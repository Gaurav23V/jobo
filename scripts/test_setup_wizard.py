#!/usr/bin/env python3
"""./setup.sh wizard and `jobo doctor`, end to end on the fictional person.

Runs the real wizard (jobo_setup/wizard.py) non-interactively with
setup.answers.example.json into a temp install root and HOME, then checks the
files it wrote, that a re-run changes nothing, that changed answers never
overwrite without a yes, that cron lines are added once and leave other lines
alone, that the password is never printed, and the doctor verdicts.

Nothing real is touched: PATH lookups, crontab, Ollama and subprocesses go
through fakes (the regression harness refuses real ones anyway).

Run: bash scripts/run_regression_tests.sh test_setup_wizard
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobo_setup import doctor, wizard  # noqa: E402
from policy import personal  # noqa: E402

ANSWERS = json.loads((REPO / "setup.answers.example.json").read_text())
PASSWORD = ANSWERS["account_password"]
results: list[tuple[str, bool]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:64s} {note}")


class Fakes:
    def __init__(self, base: Path):
        self.home = base / "home"
        self.home.mkdir()
        bins = base / "bin"
        bins.mkdir()
        self.bins = {}
        for name in ("uv", "claude", "hermes", "ollama", "crontab", "chromium", "latexmk"):
            p = bins / name
            p.write_text("#!/bin/sh\nexit 0\n")
            self.bins[name] = str(p)
        self.crontab_text: str | None = ""
        self.calls: list[tuple[list[str], dict]] = []
        self.probe = doctor.Probe(
            which=lambda n: self.bins.get(n),
            crontab=lambda: self.crontab_text,
            ollama_tags=lambda host: ["qwen2.5:3b", "gemma3:4b"],
            home=lambda: self.home,
        )

    def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv[:2] == ["crontab", "-"]:
            self.crontab_text = kw.get("input", "")
        if argv[0] == "latexmk":  # "build" the resume into -outdir
            outdir = next(a.split("=", 1)[1] for a in argv if a.startswith("-outdir="))
            (Path(outdir) / "resume.pdf").write_bytes(b"%PDF-1.5 fake")

        class Done:
            returncode = 0
        return Done()


def make_root(base: Path) -> Path:
    root = base / "jobo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO / ".env.example", root / ".env.example")
    return root


def run_wizard(root: Path, fakes: Fakes, answers: dict, *, interactive=False, inputs=None):
    out: list[str] = []
    wiz = wizard.Wizard(
        root, answers=answers, interactive=interactive, skip_installs=True,
        personal_dir=root / "personal", probe=fakes.probe, run=fakes.run,
        input_fn=inputs or (lambda q: ""), getpass_fn=lambda q: PASSWORD, out=out.append,
    )
    code = wiz.run_all()
    return code, wiz, "\n".join(out)


def statuses(wiz) -> dict[str, str]:
    return {name: status for name, status, _ in wiz.report}


def main() -> int:
    saved_env = dict(os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fakes = Fakes(base)
        os.environ["JOBO_HERMES_BIN"] = fakes.bins["hermes"]
        os.environ["JOBO_BROWSER_PROFILE"] = str(base / "browser-profile")
        os.environ.pop(personal.PASSWORD_ENV, None)
        root = make_root(base)
        pdir = root / "personal"
        try:
            print("1. non-interactive run with the fictional answers")
            code, wiz, out = run_wizard(root, fakes, ANSWERS)
            st = statuses(wiz)
            check("wizard + doctor --offline exit 0", code == 0, out.splitlines()[-1])
            for name in (personal.CANDIDATE_FILE, personal.FACTS_FILE, personal.PROFILE_FILE,
                         personal.RESUME_TEX_FILE, personal.RESUME_PDF_FILE, personal.SECRETS_FILE):
                check(f"wrote personal/{name}", (pdir / name).is_file())
            cand = json.loads((pdir / personal.CANDIDATE_FILE).read_text())
            check("candidate.json fields from answers",
                  cand["full_name"] == "Alex Example" and cand["email"] == "alex.example@example.com"
                  and cand["upload_slug"] == "Alex_Example")
            school = cand["answer_chains"]["school"]
            check("school chain: options in order, lowercase triggers",
                  school["options"][0] == "Lakeside Institute of Information Technology, Springfield"
                  and "liit springfield" in school["triggers"]
                  and all(t == t.lower() for t in school["triggers"]))
            facts = (pdir / personal.FACTS_FILE).read_text()
            check("candidate_facts.md has the header and key facts",
                  facts.startswith("Candidate facts (single source of truth - never contradict these):")
                  and "GPA: 3.5" in facts and "Available to join: 30 days notice." in facts
                  and "Sample Labs" in facts)
            mode = stat.S_IMODE((pdir / personal.SECRETS_FILE).stat().st_mode)
            check("secrets.json is mode 600", mode == 0o600, oct(mode))
            check("password stored", json.loads((pdir / personal.SECRETS_FILE).read_text())["account_password"] == PASSWORD)
            check("password never printed", PASSWORD not in out)
            env = (root / ".env").read_text()
            check(".env from .env.example + usage limits and model",
                  "JOBO_SESSION_THRESHOLD=0.30" in env and "JOBO_BUDGET_THRESHOLD=0.50" in env
                  and "OLLAMA_MODEL=qwen2.5:3b" in env and "# Module 4" in env)
            check(".env is mode 600", stat.S_IMODE((root / ".env").stat().st_mode) == 0o600)
            check("login steps skipped and reported",
                  st.get("gmail sign-in") == "SKIPPED" and st.get("linkedin sign-in") == "SKIPPED")
            check("cron not installed without install_cron", st.get("cron") == "SKIPPED"
                  and not any(c[0][0] == "crontab" for c in fakes.calls))
            check("only subprocess: latexmk builds resume.pdf",
                  [c[0][0] for c in fakes.calls] == ["latexmk"]
                  and (pdir / personal.RESUME_PDF_FILE).read_bytes() == b"%PDF-1.5 fake", str(fakes.calls))
            fakes.calls.clear()
            check("browser profile dir created", (base / "browser-profile").is_dir())
            saved = json.loads((pdir / wizard.SAVED_ANSWERS).read_text())
            check("saved answers omit the password", "account_password" not in saved)

            print("2. doctor verdicts")
            os.environ["JOBO_PERSONAL_DIR"] = str(pdir)
            checks = {c.name: c for c in doctor.run_checks(root, offline=True, probe=fakes.probe)}
            check("offline: login items SKIP (needs login)",
                  all(checks[n].status == doctor.NEEDS_LOGIN
                      for n in ("gmail client", "gmail token", "linkedin session", "hermes jobo profile")))
            check("fictional person flagged as WARN", checks["candidate.json"].status == doctor.WARN)
            check("cron + data repo are optional WARNs",
                  checks["cron"].status == doctor.WARN and checks["data backup repo"].status == doctor.WARN)
            online = doctor.run_checks(root, offline=False, probe=fakes.probe)
            check("online: missing Gmail token is MISSING, exit 1",
                  {c.name: c.status for c in online}["gmail token"] == doctor.MISSING
                  and doctor.exit_code(online) == 1)
            data = json.loads(doctor.to_json(list(checks.values()), offline=True))
            check("--json output parses, ok=true", data["ok"] is True and len(data["checks"]) == len(checks))
            check("doctor never prints the password",
                  PASSWORD not in doctor.render(online) and PASSWORD not in doctor.to_json(online))
            os.environ["JOBO_PERSONAL_DIR"] = str(personal.EXAMPLE_DIR)
            ex = {c.name: c.status for c in doctor.run_checks(root, offline=True, probe=fakes.probe)}
            check("example dir in use -> personal config MISSING", ex["personal config"] == doctor.MISSING)
            os.environ["JOBO_PERSONAL_DIR"] = str(pdir)

            print("3. re-run is idempotent")
            before = {p.name: p.read_bytes() for p in pdir.iterdir()}
            env_before = (root / ".env").read_bytes()
            code, wiz, out = run_wizard(root, fakes, ANSWERS)
            st = [s for _, s, _ in wiz.report]
            check("second run exit 0", code == 0)
            check("second run wrote nothing", "WROTE" not in st and "SET" not in st, ",".join(sorted(set(st))))
            check("files byte-identical", before == {p.name: p.read_bytes() for p in pdir.iterdir()}
                  and env_before == (root / ".env").read_bytes())

            print("4. changed answers never overwrite without a yes")
            changed = {**ANSWERS, "phone": "+1 555 0199"}
            code, wiz, _ = run_wizard(root, fakes, changed)
            check("overwrite=false keeps candidate.json",
                  statuses(wiz)["candidate.json"] == "KEPT"
                  and "+1 555 0100" in (pdir / personal.CANDIDATE_FILE).read_text())
            code, wiz, _ = run_wizard(root, fakes, {**changed, "overwrite": True})
            check("overwrite=true replaces it",
                  "+1 555 0199" in (pdir / personal.CANDIDATE_FILE).read_text())
            seen: list[str] = []
            def no_to_all(q):  # Enter on questions, "n" on every yes/no
                seen.append(q)
                return "n" if "[y/N]" in q or "[Y/n]" in q else ""
            run_wizard(root, fakes, ANSWERS, interactive=True, inputs=no_to_all)
            check("interactive 'n' to overwrite keeps the file",
                  "+1 555 0199" in (pdir / personal.CANDIDATE_FILE).read_text()
                  and any("Overwrite?" in q for q in seen))
            check("interactive shows saved values as defaults",
                  any(q.startswith("Full name [Alex Example]") for q in seen))
            check("interactive 'n' never opened a browser or ran sign-in",
                  not any(c[0][-1].endswith("auth_manual.py") for c in fakes.calls))

            print("5. cron install is idempotent and keeps other lines")
            other = "15 3 * * * /usr/bin/true # someone else's job\n"
            fakes.crontab_text = other + wizard.cron_lines(root)[0] + "\n"
            fakes.calls.clear()
            run_wizard(root, fakes, {**ANSWERS, "install_cron": True})
            lines = fakes.crontab_text.splitlines()
            check("one crontab write", [c[0] for c in fakes.calls] == [["crontab", "-"]])
            check("other line kept once, each jobo line once",
                  lines.count(other.strip()) == 1
                  and all(lines.count(w) == 1 for w in wizard.cron_lines(root)), str(lines))
            fakes.calls.clear()
            code, wiz, _ = run_wizard(root, fakes, {**ANSWERS, "install_cron": True})
            check("re-run: no crontab write", fakes.calls == [] and statuses(wiz)["cron"] == "UNCHANGED")
            check("doctor sees cron OK",
                  {c.name: c.status for c in doctor.run_checks(root, offline=True, probe=fakes.probe)}["cron"] == doctor.OK)

            print("6. resume checks and data repo")
            bad = base / "bad.tex"
            bad.write_text("\\documentclass{article}\\begin{document}\\section{Projects}\\end{document}\n")
            fakes.calls.clear()
            code, wiz, _ = run_wizard(root, fakes, {**ANSWERS, "resume_tex": str(bad), "data_repo": "alex/jobo-data"})
            st = statuses(wiz)
            check("resume without Programming Skills is refused", st["resume.tex"] == "FAIL" and code == 1)
            check("JOBO_DATA_REPO set, gh repo create skipped",
                  "JOBO_DATA_REPO=alex/jobo-data" in (root / ".env").read_text()
                  and st["gh repo create"] == "SKIPPED" and fakes.calls == [])
            env_lines = (root / ".env").read_text().splitlines()
            check(".env keys not duplicated", sum(l.startswith("JOBO_DATA_REPO=") for l in env_lines) == 1)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            personal.reload()

    passed = sum(ok for _, ok in results)
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
