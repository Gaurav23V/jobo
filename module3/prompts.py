"""System and user prompt templates for Gemini (no secrets)."""

FIT_SYSTEM = """You are an assistant helping a software engineer decide whether to apply \
to a job and how to present their background. Follow instructions precisely. \
Do not invent employers, degrees, dates, or projects not present in the candidate profile."""


def fit_user_prompt(*, profile_markdown: str, job_bundle: str) -> str:
    return f"""Candidate profile (markdown):

---
{profile_markdown}
---

Job opening (structured):

---
{job_bundle}
---

Respond with JSON only (schema enforced by the API). Score fit 0-5, explain in reasoning, \
and list highlighted_project_names using exact ## headings from the profile projects section."""


MATERIALS_SYSTEM = """You adjust a LaTeX resume and write a short cover letter. \
Rules:
- Never fabricate employers, dates, degrees, metrics, or projects.
- You may reorder, select, or lightly rephrase project/skill bullets only when grounded in \
the provided profile and project excerpts.
- Education and experience sections: do not change factual content unless the user text \
explicitly allows typos/whitespace fixes only.
- If the base resume already fits, set resume_needs_update false and resume_latex empty.
- cover_letter: concise, role-specific, plain language (markdown allowed)."""


def materials_user_prompt(
    *,
    job_bundle: str,
    base_resume_latex: str,
    project_excerpts: str,
) -> str:
    excerpts_block = project_excerpts.strip() or "(no project excerpts; use profile only)"
    return f"""Job opening:

---
{job_bundle}
---

Highlighted project excerpts (markdown):

---
{excerpts_block}
---

Base resume (full LaTeX — return a full document in resume_latex only if tailoring):

---
{base_resume_latex}
---

Produce JSON only. If resume_needs_update is true, resume_latex must be the complete LaTeX \
document. If false, resume_latex must be empty. Always fill cover_letter."""
