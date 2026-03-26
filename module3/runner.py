"""Orchestrate Module 3 triage: fit call, then materials + PDFs when applying."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from module3 import (
    constants,
    context_loader,
    gemini_client,
    job_bundle,
    pdf,
    prompts,
    persist,
    query,
)
from module3.schema import FitDecisionOutput, MaterialsOutput

logger = logging.getLogger(__name__)


@dataclass
class Module3Result:
    attempted: int = 0
    phase1_run: int = 0
    phase1_skipped: int = 0
    phase2_run: int = 0
    phase2_skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _paths_complete(job) -> bool:
    """True when both Module 3 PDF paths are set — phase 2 outputs exist; skip re-generation."""
    r = (job.module3_resume_pdf_path or "").strip()
    c = (job.module3_cover_pdf_path or "").strip()
    return bool(r and c)


def run_module3(
    session: Session,
    *,
    dry_run: bool = False,
    force: bool = False,
    min_score: int | None = None,
) -> Module3Result:
    threshold = min_score if min_score is not None else constants.min_fit_score_apply()
    profile_path = constants.profile_context_path()
    jobs = query.list_jobs_for_module3(session, force=force)
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

    for job in jobs:
        result.attempted += 1
        jid = job.id
        try:
            bundle = job_bundle.build_job_bundle_text(job)

            if job.module3_fit_score is not None and not force:
                result.phase1_skipped += 1
            else:
                fit = gemini_client.generate_structured(
                    system_instruction=prompts.FIT_SYSTEM,
                    user_text=prompts.fit_user_prompt(
                        profile_markdown=profile_md,
                        job_bundle=bundle,
                    ),
                    response_model=FitDecisionOutput,
                )
                should_apply = fit.fit_score >= threshold
                hl_json = json.dumps(
                    fit.highlighted_project_names,
                    ensure_ascii=False,
                )
                persist.apply_phase1_fit(
                    session,
                    job,
                    fit_score=fit.fit_score,
                    reasoning=fit.reasoning or "",
                    highlighted_projects_json=hl_json,
                    should_apply=should_apply,
                    dry_run=dry_run,
                )
                result.phase1_run += 1

            if not job.should_apply:
                continue

            # Phase 2 idempotency: skip materials/PDFs if paths already saved (--force reruns).
            if _paths_complete(job) and not force:
                result.phase2_skipped += 1
                continue

            raw_hl = job.module3_highlighted_projects or "[]"
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

            mats = gemini_client.generate_structured(
                system_instruction=prompts.MATERIALS_SYSTEM,
                user_text=prompts.materials_user_prompt(
                    job_bundle=bundle,
                    introduction_markdown=intro_md,
                    base_resume_latex=base_tex,
                    project_excerpts=project_excerpts,
                ),
                response_model=MaterialsOutput,
            )

            art_dir = constants.artifacts_dir() / str(job.id)
            latex_body = (mats.resume_latex or "").strip()
            if latex_body:
                pdf_resume = pdf.compile_latex_to_pdf(
                    latex_body,
                    art_dir,
                    stem="resume",
                )
                resume_path_str = str(pdf_resume.resolve())
            else:
                resume_path_str = str(default_pdf_path.resolve())

            cover_pdf = art_dir / "cover.pdf"
            pdf.cover_to_pdf(mats.cover_letter or "", cover_pdf)
            cover_path_str = str(cover_pdf.resolve())

            persist.apply_phase2_paths(
                session,
                job,
                resume_pdf_path=resume_path_str,
                cover_pdf_path=cover_path_str,
                dry_run=dry_run,
            )
            result.phase2_run += 1

        except Exception as e:
            result.failed += 1
            result.errors.append(f"job_id={jid} url={job.job_url}: {e}")
            logger.exception("Triage failed for job_id=%s", jid)

    return result
