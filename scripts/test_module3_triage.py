#!/usr/bin/env python
"""Module 3 triage after the move from Hermes to direct Claude Code calls.

Covers the four changes made together on 2026-09-28:
  * claude_client: the lean `claude -p` command and its JSON envelope parsing
  * build_fit_profile: fit calls see a one-line-per-project index
  * resume_splice: the model returns only Skills/Projects; code splices them in
  * runner: one fit call warms the cache before the rest run in parallel, the
    dead-listing preflight runs before materials, and a resume that stays two
    pages after one retry falls back to the default

Runs against a temporary SQLite DB and a temporary artifacts dir. Never
touches data/jobs.db and never makes a real model call (a fake `claude`
binary and a fake llm function stand in). Does compile real LaTeX.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module3 import (  # noqa: E402
    claude_client,
    context_loader,
    llm,
    pdf,
    prompts,
    resume_guard,
    resume_splice,
    runner,
)
from module3.schema import (  # noqa: E402
    FitDecisionOutput,
    MaterialsOutput,
    ResumeSectionsOutput,
)
from module4 import preflight  # noqa: E402

PASS = 0
FAIL = 0

BASE_TEX_PATH = REPO / "personal.example" / "resume.tex"
DEFAULT_PDF_PATH = REPO / "personal.example" / "resume.pdf"
PROFILE_PATH = REPO / "scripts" / "fixtures" / "module3_profile.md"
BASE_TEX = BASE_TEX_PATH.read_text(encoding="utf-8")


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


# --------------------------------------------------------------------------
# resume_splice
# --------------------------------------------------------------------------

EXTRA_PROJECT_MARK = "Overflow Project"


def _extra_project_block(bloat: bool = False) -> str:
    """The base Projects block with one more project appended at the end, the
    shape that pushed 13 of 15 resumes onto page 2 in the 2026-09-29 run.
    bloat=True also marks it two pages for FakePdf."""
    block = resume_splice.editable_blocks(BASE_TEX)["Projects"]
    text = "very long bullet " if bloat else ""
    entry = (
        f"    \\resumeSubItem{{{EXTRA_PROJECT_MARK}}}{{\n"
        "      \\vspace{-3pt}\\begin{itemize}[topsep=0pt]\n"
        f"        \\item Added an extra {text}project so the page runs over by its 3-4 lines.\n"
        "        \\item A second bullet, as most projects in the base have two.\n"
        "      \\end{itemize}\n"
        "    }\n\n"
    )
    end = block.rfind("\\resumeSubHeadingListEnd")
    return block[:end].rstrip() + "\n\n" + entry + "  " + block[end:]


def test_splice():
    print("\n[1] resume_splice: only Skills/Projects can change")
    blocks = resume_splice.editable_blocks(BASE_TEX)
    check("both editable blocks found", set(blocks) == set(resume_splice.EDITABLE_SECTIONS))
    check(
        "blocks end at their list, not the next section's comment",
        all(b.rstrip().endswith("\\resumeSubHeadingListEnd") for b in blocks.values()),
    )
    check("empty blocks = base unchanged", resume_splice.splice(BASE_TEX, {}) == BASE_TEX)
    check("base blocks = base unchanged", resume_splice.splice(BASE_TEX, blocks) == BASE_TEX)

    new_projects = blocks["Projects"].replace("Built a zero-latency", "Wrote a low-latency")
    out = resume_splice.splice(BASE_TEX, {"Projects": new_projects})
    check("projects edit lands", "Wrote a low-latency" in out)
    check(
        "locked sections untouched",
        resume_guard.find_locked_section_violations(BASE_TEX, out) == [],
    )
    check("skills untouched", blocks["Programming Skills"] in out)

    headerless = new_projects.split("\n", 1)[1]
    out2 = resume_splice.splice(BASE_TEX, {"Projects": headerless})
    # Indentation of the first line may differ; LaTeX ignores it.
    check(
        "missing \\section header is added back",
        out2.count("\\section{Projects}") == 1
        and " ".join(out2.split()) == " ".join(out.split()),
    )

    fenced = f"```latex\n{new_projects}\n```"
    check("markdown fences stripped", resume_splice.splice(BASE_TEX, {"Projects": fenced}) == out)

    dashed = new_projects.replace("Wrote a low-latency", "Wrote — a low-latency")
    out3 = resume_splice.splice(BASE_TEX, {"Projects": dashed})
    check("em dashes replaced, not retried", resume_guard.find_em_dashes(out3) == 0)

    # Project cap: the base page fits exactly its own number of projects.
    base_n = resume_splice.count_projects(blocks["Projects"])
    check("base resume has projects to count", base_n >= 1, str(base_n))
    extra = _extra_project_block()
    check("extra-project block has one more entry", resume_splice.count_projects(extra) == base_n + 1)
    trimmed = resume_splice.trim_projects(extra, base_n)
    check("trim keeps the first N entries", resume_splice.count_projects(trimmed) == base_n
          and "Built a zero-latency" in trimmed and EXTRA_PROJECT_MARK not in trimmed)
    check("trimmed block still closes its list",
          trimmed.rstrip().endswith("\\resumeSubHeadingListEnd"))
    check("trimmed block splices and keeps locked sections",
          resume_guard.find_locked_section_violations(
              BASE_TEX, resume_splice.splice(BASE_TEX, {"Projects": trimmed})) == [])
    check("trim is a no-op at or under the cap",
          resume_splice.trim_projects(blocks["Projects"], base_n) == blocks["Projects"])
    mats_sys = prompts.materials_system(introduction_markdown="intro", base_resume_latex=BASE_TEX)
    check("materials prompt states the exact project count",
          f"exactly {base_n} project entries" in mats_sys)

    for label, bad in (
        ("extra \\section rejected", new_projects + "\n\\section{Experience}\nhacked"),
        ("wrong section rejected", "\\section{Experience}\nhacked"),
        ("\\end{document} rejected", new_projects + "\n\\end{document}"),
    ):
        try:
            resume_splice.splice(BASE_TEX, {"Projects": bad})
            check(label, False, "no error raised")
        except ValueError:
            check(label, True)


def test_line_budget():
    print("\n[1b] line budget: Skills lines and bullets no longer than the base's (GHO-104)")
    blocks = resume_splice.editable_blocks(BASE_TEX)
    skills, projects = blocks["Programming Skills"], blocks["Projects"]
    lines = resume_splice.item_texts(skills)
    check("printed text drops markup, keeps escapes",
          lines[0].startswith("Languages & Frameworks: Python,") and "\\" not in lines[0] and "{" not in lines[0],
          repr(lines[0]))
    bullets = resume_splice.item_texts(projects)
    check("project bullets found, titles and links not counted",
          len(bullets) == 4 and not any("github" in b.lower() for b in bullets), str(bullets))
    budget = resume_splice.line_budget(BASE_TEX)
    check("budget measured from the base",
          budget == resume_splice.LineBudget(
              skills_lines=len(lines), skills_chars=max(map(len, lines)),
              bullets=4, bullet_chars=max(map(len, bullets))), str(budget))
    check("base blocks are within budget", resume_splice.over_budget(skills, projects, budget) == [])

    long_line = skills.replace(", Docker}", ", Docker, Kafka, Kubernetes}", 1)
    problems = resume_splice.over_budget(long_line, "", budget)
    check("over-long Skills line named", len(problems) == 1 and "Tools & Platforms" in problems[0]
          and f"max {budget.skills_chars}" in problems[0], str(problems))
    check("trim drops the last entries until the line fits",
          resume_splice.trim_skills(long_line, budget.skills_chars) == skills)
    # "AWS (SQS, Lambda, S3)" is one entry: never cut inside the parentheses.
    tight = resume_splice.trim_skills(long_line, len("Tools & Platforms: PostgreSQL, Redis, AWS (SQS, Lambda, S3)"))
    check("trim keeps parenthesised entries whole",
          "AWS (SQS, Lambda, S3)}" in tight and "Git" not in tight.split("Tools")[1], tight)
    check("trim leaves lines within budget alone",
          resume_splice.trim_skills(skills, budget.skills_chars) == skills)
    check("trimmed block still splices", "Kafka" not in resume_splice.splice(
        BASE_TEX, {"Programming Skills": resume_splice.trim_skills(long_line, budget.skills_chars)}))
    unbraced = "\\section{Programming Skills}\n\\begin{itemize}\n\\item \\textbf{Tools}: Git, Docker, Kafka, Redis, Linux, Bash, Make, Nginx, Kubernetes, Terraform, Helm, Vault, Consul, Nomad\n\\end{itemize}"
    trimmed = resume_splice.trim_skills(unbraced, budget.skills_chars)
    check("trim works on a line without a value group",
          len(resume_splice.item_texts(trimmed)[0]) <= budget.skills_chars and trimmed.endswith("\\end{itemize}")
          and "Tools}: Git, Docker" in trimmed, trimmed)

    long_bullet = projects.replace("Added a monthly summary", "Added a monthly summary " + "and more " * 30, 1)
    problems = resume_splice.over_budget("", long_bullet, budget)
    check("over-long bullet named", len(problems) == 1 and problems[0].startswith('Projects bullet "Added a monthly'),
          str(problems))
    extra_bullet = projects.replace("\\end{itemize}", "  \\item One more bullet.\n      \\end{itemize}", 1)
    check("extra bullet counted", resume_splice.over_budget("", extra_bullet, budget)
          == ["Projects has 5 bullets; the page holds 4."])

    mats_sys = prompts.materials_system(introduction_markdown="intro", base_resume_latex=BASE_TEX)
    check("materials prompt states the line budget on the first try",
          f"at most {budget.skills_lines} lines" in mats_sys
          and f"each at most {budget.skills_chars} characters" in mats_sys
          and f"at most {budget.bullets} bullets in total, each at most {budget.bullet_chars} characters" in mats_sys)


# --------------------------------------------------------------------------
# build_fit_profile
# --------------------------------------------------------------------------


def test_fit_profile():
    print("\n[2] build_fit_profile: compact index, same project names")
    full = PROFILE_PATH.read_text(encoding="utf-8")
    compact = context_loader.build_fit_profile(full)
    names = context_loader.list_project_names(full)
    check("under a third of the full profile", len(compact) < len(full) / 3,
          f"{len(compact)} vs {len(full)}")
    head = full[: full.index("# Projects")].rstrip()
    check("intro/experience/skills kept verbatim", compact.startswith(head))
    index_lines = [ln for ln in compact.splitlines() if ln.startswith("- `")]
    check("one index line per project", len(index_lines) == len(names),
          f"{len(index_lines)} vs {len(names)}")
    index_names = [ln[3 : ln.index("`", 3)] for ln in index_lines]
    check("index names are the exact headings", index_names == names)
    check(
        "every index name resolves to its full body for materials",
        all(context_loader.get_project_bodies(full, [n]) for n in index_names),
    )
    check("no index line is huge", max(len(ln) for ln in index_lines) < 600)
    dup = context_loader.get_project_bodies(full, ["RelayDelivery Service", "relay-service"])
    check("two names for one project send its body once", dup.count("## relay-service") == 1)
    check("no ## headings leak into the index", "\n## " not in compact[len(head):])


# --------------------------------------------------------------------------
# claude_client
# --------------------------------------------------------------------------


def _envelope(structured=None, *, result="", is_error=False, usage=None):
    return json.dumps(
        {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "result": result,
            "structured_output": structured,
            "usage": usage
            or {
                "input_tokens": 10,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "output_tokens": 50,
            },
        }
    )


def test_claude_parse():
    print("\n[3] claude_client: command and envelope parsing")
    cmd = claude_client.build_command(
        system_prompt_file=Path("/tmp/x.md"),
        schema=FitDecisionOutput.model_json_schema(),
        model="sonnet",
    )
    joined = " ".join(cmd)
    check("tools disabled", cmd[cmd.index("--tools") + 1] == "")
    check("no settings/CLAUDE.md", cmd[cmd.index("--setting-sources") + 1] == "")
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        check(f"{flag} set", flag in cmd)
    check("--bare not used (needs an API key, not the subscription)", "--bare" not in cmd)
    check("json output", "--output-format json" in joined)
    schema_arg = json.loads(cmd[cmd.index("--json-schema") + 1])
    check("schema passed", "fit_score" in schema_arg.get("properties", {}))
    check("no --max-turns 1 (structured output needs 2 turns)", "--max-turns" not in cmd)

    before = claude_client.usage_snapshot()
    fit = claude_client.parse_response(
        _envelope({"fit_score": 4, "reasoning": "ok", "highlighted_project_names": ["budget-bot"]}),
        FitDecisionOutput,
    )
    check("structured_output parsed", fit.fit_score == 4 and fit.highlighted_project_names == ["budget-bot"])
    after = claude_client.usage_snapshot()
    check("usage recorded", after.calls == before.calls + 1
          and after.cache_read_tokens == before.cache_read_tokens + 1000)

    fit2 = claude_client.parse_response(
        _envelope(None, result='```json\n{"fit_score": 2, "reasoning": "meh"}\n```'),
        FitDecisionOutput,
    )
    check("falls back to JSON in result text", fit2.fit_score == 2)

    try:
        claude_client.parse_response(_envelope(None, result="rate limited", is_error=True), FitDecisionOutput)
        check("is_error raises", False, "no error")
    except RuntimeError:
        check("is_error raises", True)

    try:
        claude_client.parse_response(_envelope({"fit_score": 9}), FitDecisionOutput)
        check("out-of-range score rejected", False, "no error")
    except Exception:
        check("out-of-range score rejected", True)


def test_claude_subprocess():
    print("\n[4] claude_client: subprocess call, retry, stdin, system file, cwd")
    # The isolated regression runner refuses every subprocess that is not a
    # browser, so subprocess.run is faked here; the code around it is real.
    calls = []

    def fake_run(cmd, *, input, capture_output, text, timeout, cwd):
        sysfile = cmd[cmd.index("--system-prompt-file") + 1]
        calls.append({"args": cmd, "stdin": input, "cwd": cwd,
                      "system": Path(sysfile).read_text(encoding="utf-8")})
        if len(calls) == 1:
            out = json.dumps({"is_error": True, "subtype": "error", "result": "overloaded"})
        else:
            out = json.dumps({"is_error": False, "subtype": "success", "result": "",
                              "structured_output": {"fit_score": 3, "reasoning": "fine"},
                              "usage": {"input_tokens": 5, "output_tokens": 7}})
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    saved = (claude_client.subprocess, claude_client._claude_bin, claude_client.time.sleep)
    claude_client.subprocess = SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    claude_client._claude_bin = lambda: "/usr/bin/claude"
    claude_client.time.sleep = lambda _s: None
    try:
        out = claude_client.generate_structured(
            system_instruction="SYSTEM TEXT",
            user_text="USER TEXT",
            response_model=FitDecisionOutput,
            model="haiku",
        )
    finally:
        claude_client.subprocess, claude_client._claude_bin, claude_client.time.sleep = saved
    check("retried after an error envelope", len(calls) == 2, f"{len(calls)} calls")
    check("result validated", out.fit_score == 3)
    check("user text on stdin", calls[-1]["stdin"] == "USER TEXT")
    check("system prompt via file", calls[-1]["system"] == "SYSTEM TEXT")
    check("model passed through", calls[-1]["args"][calls[-1]["args"].index("--model") + 1] == "haiku")
    check("runs outside the repo (no project CLAUDE.md)",
          not str(calls[-1]["cwd"]).startswith(str(REPO)), str(calls[-1]["cwd"]))
    check("scratch dir removed afterwards", not Path(calls[-1]["cwd"]).exists())


# --------------------------------------------------------------------------
# runner, end to end with a fake LLM
# --------------------------------------------------------------------------


def make_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), tmp.name


def new_job(session, jid: int, company: str, title_suffix: str = "") -> JobModel:
    job = JobModel(
        id=jid,
        company_name=company,
        job_title=f"Backend Engineer {jid}{title_suffix}",
        job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False,
        referred_json="[]",
        metadata_json=json.dumps({"stack": ["Python"]}),
        module2_attempted=True,
        module2_enriched_at=datetime.now(),
        module4_attempts=0,
    )
    session.add(job)
    session.commit()
    return job


# job_id -> scenario
FIT_SCORES = {1: 4, 2: 1, 3: 5, 4: 4, 5: 4, 6: 4, 7: 5, 8: 4, 9: 4, 10: 4}  # 7 is an internship
CLOSED = {3}


def _bloated_projects() -> str:
    block = resume_splice.editable_blocks(BASE_TEX)["Projects"]
    item = (
        "        \\item Shipped a very long bullet that exists only to push this résumé "
        "well past one page so the page-count check has something to catch.\n"
    )
    return block.replace("\\end{itemize}", item * 25 + "      \\end{itemize}", 1)


def _bloated_skills() -> str:
    """The base Skills block with one line made long enough to overflow (the
    shape all 11 default-resume fallbacks in Oct 2026 had)."""
    block = resume_splice.editable_blocks(BASE_TEX)["Programming Skills"]
    return block.replace(", Docker}", ", Docker, very long bullet of extra skills}", 1)


def _extra_skills_line() -> str:
    """The base Skills block plus a third, short line: within the per-line
    character budget, so trimming entries cannot fix it, but a whole extra
    printed line on a full page."""
    block = resume_splice.editable_blocks(BASE_TEX)["Programming Skills"]
    extra = "   \\item{\n     \\textbf{More}{: very long bullet}\n   }\n "
    return block.replace("\\resumeSubHeadingListEnd", extra + "\\resumeSubHeadingListEnd", 1)


def _tailored_projects() -> str:
    return resume_splice.editable_blocks(BASE_TEX)["Projects"].replace(
        "Built a zero-latency", "Built a low-latency"
    )


class FakePdf:
    """Stands in for module3.pdf when subprocesses are not allowed. A resume
    counts as two pages exactly when it carries the bloat marker."""

    BLOAT = "very long bullet"

    @staticmethod
    def compile_latex_to_pdf(tex_content, out_dir, stem="resume"):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{stem}.tex").write_text(tex_content, encoding="utf-8")
        pdf_path = out_dir / f"{stem}.pdf"
        pdf_path.write_bytes(b"%PDF-fake")
        return pdf_path

    @staticmethod
    def count_pdf_pages(pdf_path):
        return 2 if FakePdf.BLOAT in pdf_path.with_suffix(".tex").read_text(encoding="utf-8") else 1

    @staticmethod
    def cover_to_pdf(text, out_pdf):
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(b"%PDF-fake")
        return out_pdf


def test_runner(real_pdf: bool):
    tools = "real LaTeX/pandoc" if real_pdf else "fake PDF tools"
    print(f"\n[5] runner ({tools}): warm-up call, preflight before materials, one retry then fallback")
    session, db_path = make_session()
    art = tempfile.TemporaryDirectory()
    calls: list[tuple] = []
    lock = threading.Lock()

    def fake_llm(*, system_instruction, user_text, response_model, model=None):
        start = time.monotonic()
        jid = int(user_text.split("Backend Engineer ", 1)[1].split()[0]) if "Backend Engineer " in user_text else -1
        if response_model is FitDecisionOutput:
            time.sleep(0.05)
            out = FitDecisionOutput(
                fit_score=FIT_SCORES[jid], reasoning="r", highlighted_project_names=["budget-bot"]
            )
        elif response_model is MaterialsOutput:
            projects = {1: _tailored_projects(), 4: _bloated_projects(), 5: _bloated_projects(),
                        6: _extra_project_block(bloat=True), 8: _tailored_projects(),
                        9: _bloated_projects(), 10: _tailored_projects()}[jid]
            skills = {8: _bloated_skills(), 9: _extra_skills_line(), 10: _extra_skills_line()}.get(jid, "")
            out = MaterialsOutput(
                resume_needs_update=True, skills_section=skills, projects_section=projects,
                cover_letter="I built Budget Bot — fast.",
            )
        elif response_model is ResumeSectionsOutput:
            # job 4 fixes it by reverting to the base block; job 5 stays bloated;
            # 10 keeps its extra Skills line; 9 overflows in both blocks, so no
            # single-block variant fits either.
            if jid == 10:
                out = ResumeSectionsOutput(skills_section=_extra_skills_line(), projects_section=_tailored_projects())
            elif jid == 9:
                out = ResumeSectionsOutput(skills_section=_extra_skills_line(), projects_section=_bloated_projects())
            else:
                out = ResumeSectionsOutput(projects_section="" if jid == 4 else _bloated_projects())
        else:
            raise AssertionError(response_model)
        with lock:
            calls.append((response_model.__name__, jid, start, time.monotonic(), system_instruction, user_text))
        return out

    preflight_seen = []

    def fake_preflight(url):
        jid = int(url.rstrip("/").rsplit("/", 1)[1])
        preflight_seen.append(jid)
        if jid in CLOSED:
            return preflight.PreflightResult(closed=True, reason='page says "closed"', checked=True)
        return preflight.PreflightResult(closed=False, reason="open", checked=True)

    saved = (runner.llm.generate_structured, runner.preflight.check_job_open, runner.pdf)
    if not real_pdf:
        runner.pdf = FakePdf
    saved_env = {k: os.environ.get(k) for k in ("JOBO_BASE_RESUME_TEX", "JOBO_DEFAULT_RESUME_PDF", "JOBO_ARTIFACTS_DIR", "JOBO_TRIAGE_WORKERS", "JOBO_LLM_BACKEND")}
    runner.llm.generate_structured = fake_llm
    runner.preflight.check_job_open = fake_preflight
    os.environ.update(
        JOBO_BASE_RESUME_TEX=str(BASE_TEX_PATH),
        JOBO_DEFAULT_RESUME_PDF=str(DEFAULT_PDF_PATH),
        JOBO_ARTIFACTS_DIR=art.name,
        JOBO_TRIAGE_WORKERS="4",
    )
    os.environ.pop("JOBO_LLM_BACKEND", None)
    try:
        for jid, company in ((1, "GoodCo"), (2, "PoorFitCo"), (3, "DeadCo"), (4, "LongCo"), (5, "LongerCo"),
                            (6, "ExtraProjectCo"), (8, "LongSkillsCo"), (9, "BothLongCo"),
                            (10, "ExtraSkillsCo")):
            new_job(session, jid, company)
        new_job(session, 7, "InternCo", " Intern")
        result = runner.run_module3(session)
    finally:
        runner.llm.generate_structured, runner.preflight.check_job_open, runner.pdf = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        fits = [c for c in calls if c[0] == "FitDecisionOutput"]
        check("10 fit calls", len(fits) == 10, str(len(fits)))
        first_end = min(fits, key=lambda c: c[2])[3]
        others_start = sorted(c[2] for c in fits)[1:]
        check("first fit call finished before the others started",
              all(s >= first_end for s in others_start))
        check("fit system prompt carries the compact profile",
              "# Projects (index)" in fits[0][4] and "- **Description:**" not in fits[0][4])
        # The fake jobs are titled "Backend Engineer <id>"; the profile itself
        # may say "Backend Engineer" (the user's real title), so match the numbered form.
        check("job text is not in the shared system prompt",
              not re.search(r"Backend Engineer \d", fits[0][4]) and "job_id:" not in fits[0][4])

        check("result counters", (result.phase1_run, result.phase2_run, result.phase2_closed, result.failed) == (10, 7, 1, 0),
              str((result.phase1_run, result.phase2_run, result.phase2_closed, result.failed, result.errors)))
        check("preflight only for jobs heading to materials", sorted(preflight_seen) == [1, 3, 4, 5, 6, 8, 9, 10],
              str(sorted(preflight_seen)))

        by = lambda name, jid: [c for c in calls if c[0] == name and c[1] == jid]
        check("no materials for the dead listing", not by("MaterialsOutput", 3))
        check("no materials for the poor fit", not by("MaterialsOutput", 2))
        check("good job: one materials call, no retry",
              len(by("MaterialsOutput", 1)) == 1 and not by("ResumeSectionsOutput", 1))
        check("long job: exactly one retry", len(by("ResumeSectionsOutput", 4)) == 1)
        check("longer job: stops after one retry", len(by("ResumeSectionsOutput", 5)) == 1)
        mats_sys = by("MaterialsOutput", 1)[0][4]
        check("materials system prompt carries the base resume", "\\section{Experience}" in mats_sys)
        check("retry reuses the same (cached) system prompt", by("ResumeSectionsOutput", 4)[0][4] == mats_sys)

        rows = {j.id: j for j in session.query(JobModel).all()}
        check("poor fit not queued", rows[2].should_apply is False)
        check("internship not queued despite fit 5",
              rows[7].should_apply is False and rows[7].module3_fit_score == 5)
        check("no materials for the internship", not by("MaterialsOutput", 7))
        check("dead listing marked closed", rows[3].module4_closed_at is not None and rows[3].should_apply is False)
        check("dead listing has no materials", not rows[3].module3_resume_pdf_path)
        check("good job gets its tailored PDF",
              rows[1].module3_resume_pdf_path == str((Path(art.name) / "1" / "resume.pdf").resolve()))
        tex1 = (Path(art.name) / "1" / "resume.tex").read_text()
        check("tailored text is in the compiled source", "Built a low-latency" in tex1)
        check("tailored source keeps locked sections",
              resume_guard.find_locked_section_violations(BASE_TEX, tex1) == [])
        if real_pdf:
            check("tailored PDF really is one page",
                  pdf.count_pdf_pages(Path(rows[1].module3_resume_pdf_path)) == 1)
        check("fixed-on-retry job gets its own PDF", rows[4].module3_resume_pdf_path.endswith("/4/resume.pdf"))
        check("still-too-long job falls back to the default PDF",
              rows[5].module3_resume_pdf_path == str(DEFAULT_PDF_PATH.resolve()))
        man5 = json.loads((Path(art.name) / "5" / "manifest.json").read_text())
        check("fallback recorded as untailored", man5["resume_tailored"] is False)
        check("extra project dropped in code, no model retry", not by("ResumeSectionsOutput", 6))
        check("extra-project job keeps its tailored PDF", rows[6].module3_resume_pdf_path.endswith("/6/resume.pdf"))
        tex6 = (Path(art.name) / "6" / "resume.tex").read_text()
        check("dropped the last (least relevant) project, kept the rest",
              EXTRA_PROJECT_MARK not in tex6 and "Built a zero-latency" in tex6
              and resume_splice.count_projects(resume_splice.editable_blocks(tex6)["Projects"])
              == resume_splice.count_projects(resume_splice.editable_blocks(BASE_TEX)["Projects"]))
        if real_pdf:
            check("trimmed PDF really is one page",
                  pdf.count_pdf_pages(Path(rows[6].module3_resume_pdf_path)) == 1)
        # GHO-104: an over-long Skills line is trimmed in code on the first
        # try instead of costing a second model call.
        base_skills = resume_splice.editable_blocks(BASE_TEX)["Programming Skills"]
        check("long-skills job: trimmed in code, no model retry",
              not by("ResumeSectionsOutput", 8)
              and rows[8].module3_resume_pdf_path.endswith("/8/resume.pdf"))
        tex8 = (Path(art.name) / "8" / "resume.tex").read_text()
        check("trim dropped the last Skills entries and kept the tailored projects",
              "Built a low-latency" in tex8 and base_skills in tex8 and FakePdf.BLOAT not in tex8)
        man8 = json.loads((Path(art.name) / "8" / "manifest.json").read_text())
        check("trimmed resume recorded as tailored", man8["resume_tailored"] is True)
        check("extra-skills-line job: one retry, then a variant instead of the default",
              len(by("ResumeSectionsOutput", 10)) == 1
              and rows[10].module3_resume_pdf_path.endswith("/10/resume.pdf"))
        check("retry names the lines over budget",
              "Skills has 3 lines; the page holds 2." in by("ResumeSectionsOutput", 10)[0][5],
              by("ResumeSectionsOutput", 10)[0][5][-400:])
        tex10 = (Path(art.name) / "10" / "resume.tex").read_text()
        check("variant keeps tailored projects with the base skills",
              "Built a low-latency" in tex10 and base_skills in tex10 and FakePdf.BLOAT not in tex10)
        man10 = json.loads((Path(art.name) / "10" / "manifest.json").read_text())
        check("variant recorded as tailored", man10["resume_tailored"] is True)
        check("both blocks too long: still falls back to the default",
              rows[9].module3_resume_pdf_path == str(DEFAULT_PDF_PATH.resolve()))
        check("cover PDFs written", all(Path(rows[j].module3_cover_pdf_path).is_file() for j in (1, 4, 5, 6, 8, 9, 10)))
    finally:
        session.close()
        os.unlink(db_path)
        art.cleanup()


def test_exclusions():
    print("\n[7] exclusions: internships never queued")
    from module3 import exclusions

    for title in ("Software Engineer Intern", "Backend Internship", "SDE Interns - 2026",
                  "Full Stack Software Engineer Intern", "INTERN, Platform"):
        check(f"excluded: {title!r}", exclusions.exclusion_reason(title) is not None)
    for title in ("Internal Tools Engineer", "Software Engineer I, International Expansion",
                  "Backend Engineer", "Interface Developer", "", None):
        check(f"kept: {title!r}", exclusions.exclusion_reason(title) is None)
    profile = context_loader.load_profile_text(PROFILE_PATH)
    compact = context_loader.build_fit_profile(profile)
    check("fit profile carries the hard exclusions",
          "Internships of any kind" in compact and "9 LPA" in compact)


def test_queued_selection():
    print("\n[8] triage --queued: only the live apply queue")
    from module3 import query as m3query

    session, db_path = make_session()
    try:
        # id -> (should_apply, applied, closed, scored, has_paths)
        states = {
            1: (True, False, False, True, True),    # queued, materials done -> picked
            2: (True, False, False, True, False),   # queued, no materials -> picked
            3: (True, True, False, True, True),     # applied -> skipped
            4: (True, False, True, True, True),     # closed listing -> skipped
            5: (False, False, False, True, False),  # poor fit -> skipped
            6: (None, False, False, False, False),  # never scored -> skipped
        }
        for jid, (sa, applied, closed, scored, paths) in states.items():
            job = new_job(session, jid, f"Co{jid}")
            job.should_apply = sa
            job.applied = applied
            job.module4_closed_at = datetime.now() if closed else None
            job.module3_fit_score = 4 if scored else None
            job.module3_resume_pdf_path = "/x/r.pdf" if paths else None
            job.module3_cover_pdf_path = "/x/c.pdf" if paths else None
        session.commit()
        got = [j.id for j in m3query.list_jobs_for_module3(session, queued=True)]
        check("queued picks exactly the pending apply rows", got == [1, 2], str(got))
        got_one = [j.id for j in m3query.list_jobs_for_module3(session, queued=True, job_id=3)]
        check("queued + job_id on an applied job selects nothing", got_one == [], str(got_one))
    finally:
        session.close()
        os.unlink(db_path)


def test_backend_switch():
    print("\n[6] llm: backend switch")
    old = os.environ.get("JOBO_LLM_BACKEND")
    try:
        os.environ.pop("JOBO_LLM_BACKEND", None)
        check("claude is the default", llm.backend() == "claude")
        os.environ["JOBO_LLM_BACKEND"] = "Hermes"
        check("hermes selectable", llm.backend() == "hermes")
    finally:
        if old is None:
            os.environ.pop("JOBO_LLM_BACKEND", None)
        else:
            os.environ["JOBO_LLM_BACKEND"] = old


def main(real_pdf: bool = False) -> int:
    """Entrypoint. The isolated regression runner imports this module and calls
    main(), which forbids subprocesses, so PDF tools are faked there. Running
    the file directly also compiles real LaTeX (needs latexmk, pdfinfo, pandoc)."""
    print("module 3 triage regressions")
    test_splice()
    test_line_budget()
    test_fit_profile()
    test_claude_parse()
    test_claude_subprocess()
    test_runner(real_pdf)
    test_backend_switch()
    test_exclusions()
    test_queued_selection()
    # Report in the runner's convention ("N/N passed"); scripts/
    # run_isolated_regressions.py::check_count parses this.
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(real_pdf=True))
