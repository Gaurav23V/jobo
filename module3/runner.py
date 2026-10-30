"""Orchestrate Module 3 triage: fit call, then materials + PDFs when applying.

Phase 1 (fit) runs in parallel via a thread pool of `claude -p` subprocesses
(see module3/llm.py for the backend switch); results are persisted by the main
thread only. Phase 2 (materials + PDFs) runs sequentially afterwards so
LaTeX/pandoc and DB writes stay simple and safe.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from db.models import JobModel
from module3 import (
    claude_client,
    constants,
    context_loader,
    exclusions,
    job_bundle,
    llm,
    manifest,
    pdf,
    prompts,
    persist,
    query,
    resume_guard,
    resume_splice,
)
from module3.claude_auth import AUTH_EXIT, FIX_HINT, ClaudeAuthError
from module3.schema import FitDecisionOutput, MaterialsOutput, ResumeSectionsOutput
from module4 import persist as apply_persist
from module4 import preflight

logger = logging.getLogger(__name__)

# First try plus ONE targeted retry. Three attempts used to be the rule, but
# the run logs showed the jobs still two pages long after attempt 2 almost
# never made it on attempt 3 either - that third call was paid for and then
# thrown away in favour of the default resume anyway.
MAX_MATERIALS_ATTEMPTS = 2

# Stop the stage after this many Claude authentication failures (GHO-77): the
# login is gone and every further call fails the same way.
AUTH_FAILURE_LIMIT = 3


@dataclass
class Module3Result:
    attempted: int = 0
    phase1_run: int = 0
    phase1_skipped: int = 0
    phase2_run: int = 0
    phase2_skipped: int = 0
    phase2_closed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    llm_usage: str = ""
    # GHO-77: calls that failed on the Claude login, jobs left untouched once
    # the stage stopped on it, and the stop reason (empty = did not stop).
    auth_failures: int = 0
    auth_skipped: int = 0
    auth_error: str = ""

    @property
    def succeeded(self) -> int:
        return self.phase1_run + self.phase2_run + self.phase2_closed


@dataclass
class TriageHealth:
    label: str  # OK, FAILED, FAILED (claude_auth)
    exit_code: int  # 0, 4, claude_auth.AUTH_EXIT
    reasons: list[str] = field(default_factory=list)


def triage_health(result: Module3Result) -> TriageHealth:
    """The stage verdict from its own counters (GHO-77: 154 of 154 failed and
    the pipeline still printed "=== triage OK ===")."""
    if result.auth_error:
        return TriageHealth("FAILED (claude_auth)", AUTH_EXIT, [
            f"Claude can't authenticate: {result.auth_error[:300]}",
            f"stopped after {result.auth_failures} auth failure(s); {result.auth_skipped} job(s) "
            f"left for the next run; {FIX_HINT}",
        ])
    if result.failed and not result.succeeded:
        return TriageHealth("FAILED", 4, [
            f"every Claude call failed ({result.failed} of {result.failed}); nothing was triaged",
        ])
    return TriageHealth("OK", 0)


def _paths_complete(job) -> bool:
    """True when both Module 3 PDF paths are set — phase 2 outputs exist; skip re-generation."""
    r = (job.module3_resume_pdf_path or "").strip()
    c = (job.module3_cover_pdf_path or "").strip()
    return bool(r and c)


def _latex_errors(art_dir: Path, stem: str = "resume", limit: int = 6) -> str:
    """The `! ...` error lines (plus their `l.NN` line) from the LaTeX log, so
    a compile failure can be described to the model instead of just 'failed'."""
    log = art_dir / f"{stem}.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    out = []
    for i, line in enumerate(lines):
        if line.startswith("! "):
            out.append(line.strip())
            for nxt in lines[i + 1 : i + 4]:
                if nxt.startswith("l."):
                    out.append(nxt.strip())
                    break
        if len(out) >= limit:
            break
    return " ".join(out)


def _verify_resume(*, base_tex: str, skills: str, projects: str, art_dir: Path):
    """Splice, compile and check one candidate.

    Returns (pdf_path, latex, problems, overflowed) where overflowed is True
    only when the resume compiled but ran past one page.
    """
    try:
        latex = resume_splice.splice(
            base_tex,
            {resume_splice.SKILLS_SECTION: skills, resume_splice.PROJECTS_SECTION: projects},
        )
    except ValueError as e:
        return None, "", [str(e)], False

    # Splicing makes these impossible; kept as a cheap safety net.
    problems = list(resume_guard.find_locked_section_violations(base_tex, latex))
    em_dash_count = resume_guard.find_em_dashes(latex)
    if em_dash_count:
        problems.append(
            f"Resume LaTeX contains {em_dash_count} em dash(es) (—). Replace each with "
            "a period, comma, or colon."
        )

    try:
        pdf_path = pdf.compile_latex_to_pdf(latex, art_dir, stem="resume")
    except RuntimeError:
        detail = _latex_errors(art_dir)
        problems.append(
            "The spliced resume failed to compile"
            + (f": {detail}" if detail else "")
            + ". Check braces and \\href syntax."
        )
        return None, latex, problems, False

    overflowed = False
    try:
        pages = pdf.count_pdf_pages(pdf_path)
        if pages > 1:
            overflowed = True
            problems.append(
                f"Resume compiled to {pages} pages; MUST be exactly 1 page. "
                "Trim bullet lengths in Skills/Projects (the only sections you "
                "may edit) - shorten wording, do not remove entire bullets unless "
                "truly redundant. Each Skills line must be no longer than the "
                "same line in the base resume: one extra item wraps the line "
                "and pushes the page over."
            )
    except Exception as e:
        logger.warning("page-count check failed for %s: %s", pdf_path, e)
    return pdf_path, latex, problems, overflowed


def _verify_with_trims(
    *, base_tex: str, skills: str, projects: str, art_dir: Path, job_id: int
):
    """_verify_resume, plus no-LLM fixes for the two commonest overflows.

    The base resume fills its page with exactly its own number of projects.
    In the 2026-09-29 run, 13 of 15 tailored resumes with one extra project
    fell back to the default, every one with the last section pushed onto
    page 2, while 2-project resumes almost all fit. So when a candidate
    overflows AND carries more projects than the base, drop the trailing
    (least relevant) entries and check again before spending a model retry.

    The page also has less than one printed line spare, so a Skills line
    that grows past the longest base line wraps and spills the page (on
    2026-10-30, 58 of 104 tailored resumes overflowed on the first try).
    When the candidate still overflows, drop entries from the end of each
    over-long Skills line and check again. If the page is still over, the
    problem names the lines over budget so the model's retry fixes those.

    Returns (pdf_path, latex, problems, skills_used, projects_used).
    """
    pdf_path, latex, problems, overflowed = _verify_resume(
        base_tex=base_tex, skills=skills, projects=projects, art_dir=art_dir
    )
    if not overflowed:
        return pdf_path, latex, problems, skills, projects
    cap = resume_splice.count_projects(
        resume_splice.editable_blocks(base_tex)[resume_splice.PROJECTS_SECTION]
    )
    have = resume_splice.count_projects(projects)
    if cap and have > cap:
        try:
            trimmed = resume_splice.trim_projects(projects, cap)
        except ValueError as e:
            logger.warning("job_id=%s: could not trim projects: %s", job_id, e)
        else:
            logger.info(
                "job_id=%s: resume ran past one page with %d projects; keeping the first %d.",
                job_id, have, cap,
            )
            projects = trimmed
            pdf_path, latex, problems, overflowed = _verify_resume(
                base_tex=base_tex, skills=skills, projects=projects, art_dir=art_dir
            )
    budget = resume_splice.line_budget(base_tex)
    if overflowed and budget and skills.strip():
        trimmed = resume_splice.trim_skills(skills, budget.skills_chars)
        if trimmed != skills:
            logger.info(
                "job_id=%s: resume ran past one page; trimmed Skills lines to %d characters.",
                job_id, budget.skills_chars,
            )
            skills = trimmed
            pdf_path, latex, problems, overflowed = _verify_resume(
                base_tex=base_tex, skills=skills, projects=projects, art_dir=art_dir
            )
    if overflowed and budget:
        problems += resume_splice.over_budget(skills, projects, budget)
    return pdf_path, latex, problems, skills, projects


def _one_page_variant(
    *, base_tex: str, skills: str, projects: str, art_dir: Path, job_id: int
):
    """Keep one tailored block and the base version of the other, no LLM call.

    In the 14 days to 2026-10-15, 101 tailored resumes came out two pages on
    the first try and 11 stayed two pages after the model's fix call, so they
    shipped the untailored default. Re-splicing those 11 offline: with the
    base Skills block and the tailored Projects every one fit one page; with
    the tailored Skills and base Projects none did. The model lengthens a
    Skills line by an item or two, the line wraps, and the page is already
    full. The fix prompt says "trim bullets", so the model trims Projects and
    the Skills overflow stays.

    Tries base Skills + tailored Projects first (project tailoring is most of
    the value), then tailored Skills + base Projects. Only variants that keep
    something tailored are tried. Returns (pdf_path, latex, variant_name) for
    the first one-page variant, else None.
    """
    variants = []
    if projects.strip():
        variants.append(("base skills + tailored projects", "", projects))
    if skills.strip():
        variants.append(("tailored skills + base projects", skills, ""))
    if not (skills.strip() and projects.strip()):
        # Only one block was tailored, so dropping it means the default resume.
        return None
    for name, sk, pr in variants:
        pdf_path, latex, problems, _, _ = _verify_with_trims(
            base_tex=base_tex, skills=sk, projects=pr, art_dir=art_dir, job_id=job_id,
        )
        if not problems and pdf_path is not None:
            logger.info("job_id=%s: one page with %s.", job_id, name)
            return pdf_path, latex, name
    return None


def _compile_verified_resume(
    *,
    skills: str,
    projects: str,
    base_tex: str,
    art_dir: Path,
    materials_system: str,
    job_bundle_text: str,
    project_excerpts: str,
    job_id: int,
):
    """Splice tailored Skills/Projects into the base; verify it compiles to one page.

    On a problem, re-asks the LLM once with its own blocks and the specific
    problem named, so it can fix rather than regenerate. If that still fails,
    falls back to the DEFAULT untailored resume (guaranteed one page) rather
    than shipping a broken PDF.

    Returns (pdf_path, final_latex_used, fell_back_to_default: bool).
    """
    latex = ""
    last_problem = ""
    for attempt in range(1, MAX_MATERIALS_ATTEMPTS + 1):
        pdf_path, latex, problems, skills, projects = _verify_with_trims(
            base_tex=base_tex, skills=skills, projects=projects, art_dir=art_dir,
            job_id=job_id,
        )
        if not problems:
            if attempt > 1:
                logger.info(
                    "job_id=%s: resume fixed on attempt %d/%d.",
                    job_id, attempt, MAX_MATERIALS_ATTEMPTS,
                )
            return pdf_path, latex, False

        last_problem = "; ".join(problems)
        logger.warning(
            "job_id=%s: tailored resume attempt %d/%d failed verification: %s",
            job_id, attempt, MAX_MATERIALS_ATTEMPTS, last_problem,
        )
        if attempt == MAX_MATERIALS_ATTEMPTS:
            break

        try:
            fix = llm.generate_structured(
                system_instruction=materials_system,
                user_text=prompts.resume_fix_user_prompt(
                    job_bundle=job_bundle_text,
                    project_excerpts=project_excerpts,
                    skills_section=skills,
                    projects_section=projects,
                    problem=last_problem,
                ),
                response_model=ResumeSectionsOutput,
            )
            # An empty field means "use the base block", which is always valid.
            skills = fix.skills_section or ""
            projects = fix.projects_section or ""
        except Exception as e:
            logger.warning("job_id=%s: retry materials call failed: %s", job_id, e)
            break

    variant = _one_page_variant(
        base_tex=base_tex, skills=skills, projects=projects, art_dir=art_dir, job_id=job_id,
    )
    if variant is not None:
        pdf_path, latex, name = variant
        logger.warning(
            "job_id=%s: tailored resume still %s after %d attempts; shipping %s "
            "instead of the default resume.",
            job_id, last_problem.split(";")[0], MAX_MATERIALS_ATTEMPTS, name,
        )
        return pdf_path, latex, False

    logger.error(
        "job_id=%s: resume verification failed after %d attempts (%s); "
        "falling back to default untailored resume.",
        job_id, MAX_MATERIALS_ATTEMPTS, last_problem,
    )
    return None, latex, True


def _workers() -> int:
    raw = os.environ.get("JOBO_TRIAGE_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 12


def run_module3(
    session: Session,
    *,
    dry_run: bool = False,
    force: bool = False,
    min_score: int | None = None,
    job_id: int | None = None,
    queued: bool = False,
) -> Module3Result:
    """queued: re-run fit AND materials for every job in the apply queue
    (should_apply, not applied, not closed) and nothing else. Implies force
    for those rows."""
    threshold = min_score if min_score is not None else constants.min_fit_score_apply()
    profile_path = constants.profile_context_path()
    jobs = query.list_jobs_for_module3(session, force=force, job_id=job_id, queued=queued)
    force = force or queued
    result = Module3Result()
    if not jobs:
        return result

    try:
        profile_md = context_loader.load_profile_text(profile_path)
    except FileNotFoundError as e:
        result.errors.append(str(e))
        return result

    base_tex_path = constants.base_resume_tex_path()
    default_pdf_path = constants.default_resume_pdf_path()
    if base_tex_path is None or not base_tex_path.is_file():
        raise RuntimeError("JOBO_BASE_RESUME_TEX must point to an existing .tex file.")
    if default_pdf_path is None or not default_pdf_path.is_file():
        raise RuntimeError("JOBO_DEFAULT_RESUME_PDF must point to an existing PDF.")
    base_tex = base_tex_path.read_text(encoding="utf-8", errors="replace")
    intro_md = context_loader.get_introduction_markdown(profile_md)

    # Everything identical across jobs goes in the system prompt, which the
    # backend caches; only the job-specific text goes in the user message.
    fit_system = prompts.fit_system(
        profile_markdown=context_loader.build_fit_profile(profile_md)
    )
    materials_system = prompts.materials_system(
        introduction_markdown=intro_md, base_resume_latex=base_tex
    )

    last_auth_error = ""

    # ---------------- Phase 1: fit decisions in parallel ----------------
    to_fit = []
    apply_jobs = []
    for job in jobs:
        result.attempted += 1
        if job.module3_fit_score is not None and not force:
            result.phase1_skipped += 1
        else:
            to_fit.append(job)
            continue  # decide apply after the fit call lands
        if bool(job.should_apply):
            apply_jobs.append(job.id)

    if to_fit:
        logger.info(
            "Phase 1: %s fit calls via %s (%s workers)...",
            len(to_fit),
            llm.backend(),
            _workers(),
        )

        # Extract everything from the ORM in THIS (main) thread before handing
        # work to the pool. Worker threads must never touch Session-bound
        # objects: SQLite sessions aren't thread-safe, and a mid-flight
        # rollback leaves worker-held instances stale ("row is not present").
        tasks = [
            (
                job.id,
                job.company_name,
                job.job_url,
                job_bundle.build_job_bundle_text(job),
            )
            for job in to_fit
        ]

        def do_fit(task):
            # Worker threads only touch the LLM; all DB access stays in the
            # main thread (SQLite session is not thread-safe).
            jid, company, url, bundle = task
            fit = llm.generate_structured(
                system_instruction=fit_system,
                user_text=prompts.fit_user_prompt(job_bundle=bundle),
                response_model=FitDecisionOutput,
                model=llm.fit_model(),
            )
            return jid, company, url, fit

        done = 0
        with ThreadPoolExecutor(max_workers=_workers()) as ex:
            # Run one call on its own first: it stores the shared system prompt
            # in the cache, so the parallel calls after it read it back cheaply
            # instead of all twelve writing it at once. While those warm-up
            # calls fail on the Claude login, keep going one at a time, so a
            # dead login costs AUTH_FAILURE_LIMIT calls, not one per job.
            futures = {}
            submitted = 0
            warm_auth_failures = 0
            while submitted < len(tasks):
                warm = ex.submit(do_fit, tasks[submitted])
                futures[warm] = tasks[submitted][0]
                submitted += 1
                wait([warm])
                if not isinstance(warm.exception(), ClaudeAuthError):
                    break
                warm_auth_failures += 1
                if warm_auth_failures >= AUTH_FAILURE_LIMIT:
                    break
            if warm_auth_failures < AUTH_FAILURE_LIMIT:
                futures.update({ex.submit(do_fit, task): task[0] for task in tasks[submitted:]})
                submitted = len(tasks)
            result.auth_skipped += len(tasks) - submitted
            for fut in as_completed(futures):
                jid = futures[fut]
                try:
                    _jid, company, url, fit = fut.result()
                except CancelledError:
                    result.auth_skipped += 1
                    continue
                except Exception as e:
                    if isinstance(e, ClaudeAuthError):
                        result.auth_failures += 1
                        last_auth_error = str(e)
                        if result.auth_failures >= AUTH_FAILURE_LIMIT and not result.auth_error:
                            result.auth_error = str(e)
                            # Calls already running finish; queued ones never start.
                            for other in futures:
                                other.cancel()
                    # A failed LLM call can leave the SQLite session in a bad
                    # state; roll back so later jobs aren't poisoned by it.
                    session.rollback()
                    result.failed += 1
                    result.errors.append(f"job_id={jid}: fit: {e}")
                    logger.warning("Fit failed for job_id=%s: %s", jid, e)
                    continue
                job = session.get(JobModel, jid)
                if job is None:
                    continue
                should_apply = fit.fit_score >= threshold
                excluded = exclusions.exclusion_reason(str(job.job_title or ""))
                if excluded and should_apply:
                    logger.info(
                        "job_id=%s: fit %s but not queued: %s.", jid, fit.fit_score, excluded
                    )
                    should_apply = False
                hl_json = json.dumps(fit.highlighted_project_names, ensure_ascii=False)
                try:
                    persist.apply_phase1_fit(
                        session,
                        job,
                        fit_score=fit.fit_score,
                        reasoning=fit.reasoning or "",
                        highlighted_projects_json=hl_json,
                        should_apply=should_apply,
                        dry_run=dry_run,
                    )
                except Exception as e:
                    session.rollback()
                    result.failed += 1
                    result.errors.append(f"job_id={jid}: persist: {e}")
                    logger.warning("Persist failed for job_id=%s: %s", jid, e)
                    continue
                if should_apply:
                    apply_jobs.append(job.id)
                done += 1
                if done % 20 == 0:
                    logger.info("Phase 1 progress: %s/%s", done, len(to_fit))

        result.phase1_run = done

    # ---------------- Phase 2: materials + PDFs, sequential ----------------
    if result.auth_error and apply_jobs:
        logger.warning(
            "Claude can't authenticate; skipping materials for %s job(s).", len(set(apply_jobs))
        )
        result.auth_skipped += len(set(apply_jobs))
        apply_jobs = []
    seen = set()
    for jid in apply_jobs:
        if jid in seen:
            continue
        seen.add(jid)
        if result.auth_error:
            result.auth_skipped += 1
            continue

        fresh = session.get(JobModel, jid)
        if fresh is None:
            continue
        if not bool(fresh.should_apply):
            continue
        if _paths_complete(fresh) and not force:
            result.phase2_skipped += 1
            continue

        # Same cheap dead-listing check module4 runs before an apply agent
        # (see module4/preflight.py: only a provably closed LinkedIn posting is
        # skipped, anything ambiguous falls through). Running it here too
        # stops us paying for a resume and cover letter nobody will ever send:
        # ~150 of the first 595 material sets were for postings already dead.
        pf = preflight.check_job_open(str(fresh.job_url or ""))
        if pf.should_skip:
            apply_persist.mark_closed(
                session, fresh, status="closed", detail=pf.reason, dry_run=dry_run
            )
            result.phase2_closed += 1
            logger.info("job_id=%s: listing is closed (%s); skipping materials.", jid, pf.reason)
            continue

        bundle = job_bundle.build_job_bundle_text(fresh)
        raw_hl = fresh.module3_highlighted_projects or "[]"
        try:
            project_list = json.loads(raw_hl)
        except json.JSONDecodeError:
            project_list = []
        if not isinstance(project_list, list):
            project_list = []
        project_excerpts = context_loader.get_project_bodies(
            profile_md,
            [str(x) for x in project_list],
        )

        try:
            mats = llm.generate_structured(
                system_instruction=materials_system,
                user_text=prompts.materials_user_prompt(
                    job_bundle=bundle,
                    project_excerpts=project_excerpts,
                ),
                response_model=MaterialsOutput,
            )
        except ClaudeAuthError as e:
            result.failed += 1
            result.auth_failures += 1
            last_auth_error = str(e)
            result.errors.append(f"job_id={jid} url={fresh.job_url}: materials: {e}")
            logger.warning("Materials failed for job_id=%s: %s", jid, e)
            if result.auth_failures >= AUTH_FAILURE_LIMIT:
                result.auth_error = str(e)
            continue
        except Exception as e:
            result.failed += 1
            result.errors.append(f"job_id={jid} url={fresh.job_url}: materials: {e}")
            logger.exception("Materials failed for job_id=%s", jid)
            continue

        art_dir = constants.artifacts_dir() / str(jid)
        skills = (mats.skills_section or "").strip()
        projects = (mats.projects_section or "").strip()
        tailored = bool(skills or projects)
        fallback_to_default = False
        try:
            if tailored:
                pdf_resume, _latex, fallback_to_default = _compile_verified_resume(
                    skills=skills,
                    projects=projects,
                    base_tex=base_tex,
                    art_dir=art_dir,
                    materials_system=materials_system,
                    job_bundle_text=bundle,
                    project_excerpts=project_excerpts,
                    job_id=jid,
                )
                if fallback_to_default or pdf_resume is None:
                    resume_path_str = str(default_pdf_path.resolve())
                else:
                    resume_path_str = str(pdf_resume.resolve())
            else:
                resume_path_str = str(default_pdf_path.resolve())

            cover_pdf = art_dir / "cover.pdf"
            cover_text = (mats.cover_letter or "").replace("—", ", ").replace("–", "-")
            pdf.cover_to_pdf(cover_text, cover_pdf)
            cover_path_str = str(cover_pdf.resolve())
        except Exception as e:
            result.failed += 1
            result.errors.append(f"job_id={jid} url={fresh.job_url}: pdf: {e}")
            logger.exception("PDF generation failed for job_id=%s", jid)
            continue

        persist.apply_phase2_paths(
            session,
            fresh,
            resume_pdf_path=resume_path_str,
            cover_pdf_path=cover_path_str,
            dry_run=dry_run,
        )
        try:
            manifest.write_manifest(
                art_dir=art_dir,
                job_id=jid,
                company=fresh.company_name or "",
                title=fresh.job_title or "",
                base_resume_tex=base_tex,
                profile_markdown=profile_md,
                resume_needs_update=tailored and not fallback_to_default,
                resume_pdf_path=resume_path_str,
                cover_pdf_path=cover_path_str,
            )
        except Exception as e:  # pragma: no cover - audit trail only, never block the run
            logger.warning("Failed to write manifest for job_id=%s: %s", jid, e)
        result.phase2_run += 1

    if result.auth_failures and not result.auth_error and not result.succeeded \
            and result.auth_failures == result.failed:
        # Fewer jobs than AUTH_FAILURE_LIMIT, every one of them on the login.
        result.auth_error = last_auth_error
    if result.auth_error:
        logger.error(
            "Claude can't authenticate (claude_auth): stopped after %s auth failure(s), "
            "%s job(s) left for the next run.", result.auth_failures, result.auth_skipped,
        )

    if llm.backend() == "claude":
        usage = claude_client.usage_snapshot()
        if usage.calls:
            result.llm_usage = usage.summary()
            logger.info("Module 3 LLM usage: %s", result.llm_usage)
    return result
