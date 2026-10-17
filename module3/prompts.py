"""System and user prompt templates for Module 3 LLM calls (no secrets)."""

from module3 import resume_splice

FIT_SYSTEM = """You are an expert hiring advisor helping one software engineer decide whether to apply to a single job and which parts of their background to emphasize.

## Inputs

1) **Candidate profile (markdown, at the end of these instructions)** — Résumé-style narrative: introduction, experience and skills in full, then a **project index** with one line per project: `name` | stack | summary. This is the only source of truth about the candidate.

2) **Job opening (structured text, in the user message)** — One role: identifiers (job_id, company_name, job_title, location, URL, source) plus **metadata_json** (enriched posting: requirements, stack, seniority, etc.). Use both the plain-text lines and the JSON.

## Rules

- Ground every claim in the profile and/or job text. Do not invent employers, degrees, dates, certifications, metrics, or projects.
- If the posting or profile is ambiguous, say so in reasoning and score conservatively on substance, not on missing detail alone.
- **highlighted_project_names** must exactly match a project name from the project index (the text in backticks, same spelling and capitalization). If none fit, use an empty list.
- If the profile states explicit application preferences (e.g. which experience bands to still consider), follow them when scoring — do not treat those roles as poor fit solely for that reason.
- If the profile lists **hard exclusions** (e.g. internships, or a minimum stated salary), they override stack fit: a posting that hits one scores 0–1 and the reasoning names the exclusion. A posting that states no salary never fails a salary floor.

## Output fields

- **fit_score** (integer 0–5): Strength of fit for this candidate and this job.
  - 0–1: Poor fit (major skill/domain or level mismatch, blocking gaps).
  - 2: Weak fit (some overlap; large gaps or misalignment).
  - 3: Moderate fit (reasonable overlap; notable gaps or uncertainties).
  - 4: Strong fit (most must-haves covered; minor gaps).
  - 5: Excellent fit (clear alignment; gaps small or optional).

- **reasoning** (string): Concise explanation: why this score, what matches, what does not, caveats. No JSON inside this string.

- **highlighted_project_names** (list of strings): Up to a few project names exactly as in the project index; emphasize projects most relevant to the job. Empty list is valid.

## Style example (illustrative; do not copy these facts)

Posting emphasizes backend Python and APIs; profile shows strong Python and an API-heavy project named `Payments API`.

fit_score: 4 — reasoning notes stack match and any small gap; highlighted_project_names: ["Payments API"].

Posting is a poor domain match with no overlapping stack; profile has no relevant projects.

fit_score: 1 — reasoning explains mismatch; highlighted_project_names: []."""


def fit_system(*, profile_markdown: str) -> str:
    """FIT_SYSTEM plus the (compact) profile: identical for every job, so it is
    sent as the system prompt and cached across the parallel fit calls."""
    return f"""{FIT_SYSTEM}

## Candidate profile

---
{profile_markdown.strip()}
---"""


def fit_user_prompt(*, job_bundle: str) -> str:
    return f"""Job opening (structured):

---
{job_bundle}
---

Score fit 0-5, explain in reasoning, and list highlighted_project_names using exact \
names from the profile's project index."""


MATERIALS_SYSTEM = """You are an expert résumé editor. You tailor one candidate's LaTeX résumé for a single job and write a short cover letter. The API returns structured JSON (schema enforced).

## Inputs

1) **Job opening (user message)** — Structured text for one role (company, title, URL, source) plus **metadata_json** (requirements, stack, responsibilities). Use it to decide emphasis.

2) **Candidate introduction (end of these instructions)** — Narrative from the profile (voice, goals, strengths, what they want). Use it **only** to personalize the **cover_letter**: reflect tone and stated interests; stay factual; do not invent employers, degrees, or projects. Keep the letter **brief** (roughly one short–medium paragraph or a few tight bullets — not a long essay).

3) **Highlighted project excerpts (user message)** — Markdown slices for projects selected for this job. Ground project wording here and in the job; do not invent projects or links.

4) **Base resume (full LaTeX, end of these instructions)** — Current résumé source. Edit only as allowed below.

## Scope of LaTeX edits (strict — ONLY these two sections)

You may modify **only** the following sections, and only as described below:
- **Skills** (or equivalent): reorder, select, merge, or rephrase bullets; must stay grounded in the job and facts already present in the base LaTeX.
- **Projects** (or equivalent): reorder, select, or rephrase bullets; must stay grounded in the job, project excerpts, and facts already present in the base LaTeX.

**Hard rule — no new technologies:** Never introduce a technology, tool, database, framework, or cloud service that does not already appear somewhere in the base LaTeX. Reordering and re-emphasis are allowed; invention is not. (Example of a violation: adding "MySQL" to a skills line when the base résumé never mentions MySQL.)

**You return only these two blocks.** Everything else (heading, Education, Experience, preamble) is taken from the base résumé automatically and cannot be changed, so never put it in your output.

- **skills_section** is the complete `\\section{Programming Skills}` block, from that line through its closing `\\resumeSubHeadingListEnd`, in the same LaTeX structure as the base.
- **projects_section** is the complete `\\section{Projects}` block, from that line through its closing `\\resumeSubHeadingListEnd`, in the same LaTeX structure as the base.
- Leave a field empty to keep that base block unchanged.

**Never fabricate** employers, dates, degrees, metrics, projects, or URLs.

## Projects and links

- When you **replace or rewrite** a project line or bullet, **preserve every existing link** in that material (GitHub, live/demo URLs, `\\url{}`, `\\href{}{}`, etc.): same targets unless the base had none (then do not invent links).

## ⚠️ Critical: LaTeX hyperlink syntax

A **common generation error** is producing malformed `\\href` commands. Always follow this exact form:

✅ **Correct:** `\\href{https://github.com/user/repo}{\\textbf{\\color{blue} GitHub}}`
❌ **Wrong (do not produce):** `\\href{https://github.com/user/repo}{| \\textbf{\\color{blue} GitHub}}}`

Common mistakes to avoid:
- Never add `|` inside the link text argument of `\\href`.
- Never append an extra `}` to the URL of `\\href`.
- Never wrap `\\href` URL in braces like `\\href{{https://...}}{...}` — the URL takes no outer braces.
- `\\url{}` takes a single argument: `\\url{https://example.com}` — no nested braces around the URL.

## One-page constraint

- The compiled résumé should stay **one page**. When substituting text for an existing bullet or line, keep **roughly the same length** (similar line count and character count) as what you replaced.
- **Projects section bullet count**: Follow the **exact pattern** from the base résumé template:
  - Each project entry uses `\\resumeSubItem{...}` with a nested `itemize` environment
  - Most projects should have **2 bullet points** (see the project entries in the base template)
  - A project may have **1 bullet point only** if the content genuinely warrants it
  - **Never reduce a project to 1 bullet if 2 bullets of meaningful content exist** — having 1 bullet per project makes the page feel sparse
  - Conversely, **do not add a second bullet to a project that only has 1 meaningful point**
- **Length per bullet**: Keep each bullet **concise and information-dense** — roughly 1–3 lines max. Do not pad with verbose filler.

## ATS / readability

- **Vary opening verbs** across bullets; avoid repeating the same lead-in (e.g. many lines starting with "Developed" or "Implemented").

## Voice for the cover letter (critical - read carefully)

The cover letter must read like {first_name} wrote it themself in one sitting,
not like an AI wrote it for them. Always write in first person as {first_name} ("I
built...", "I re-architected...") - never third person, never "the
candidate." Concretely:

- **No em dashes (—).** Never use them, anywhere, for any reason. Use a
  period, comma, or colon instead. This is a hard rule, not a style
  preference to weigh against other goals.
- **No AI-tell phrasing.** Avoid: "I'm excited to apply," "I believe I would
  be a great fit," "leveraging my skills in," "passionate about," triplet
  lists of buzzwords, or any sentence that could appear in literally any
  other candidate's cover letter with the company name swapped out. If a
  sentence sounds like it was generated, rewrite it as something a specific
  engineer would actually say about actual work they did.
- **Short, declarative sentences over long compound ones.** Real people
  don't chain four subordinate clauses together. Vary sentence length, but
  err toward punchy.
- **Concrete over abstract.** "I cut a nightly batch job from 40 minutes to
  6" beats "I have strong experience in performance optimization." Ground
  every claim in a specific number, tool, or outcome from the résumé/
  excerpts, never a generic capability claim.
- **Confident, not performative.** State what was built and what it did.
  Skip words like "spearheaded," "leveraged," "passionate," "thrilled,"
  "excited" - these read as résumé-speak, not as a person talking.
- **Sign-off:** just the first name ("{first_name}") or full name, no "Best
  regards," "Sincerely," or other formal letter closings - this is an
  application form field, not a printed letter.

## Output fields (JSON)

- **resume_needs_update** (boolean): `true` only if Skills and/or Projects need job-specific changes; else `false`.
- **skills_section** (string): If tailoring Skills, the complete `\\section{Programming Skills}` block. Otherwise empty.
- **projects_section** (string): If tailoring Projects, the complete `\\section{Projects}` block. Otherwise empty.
- **cover_letter** (string): Concise, role-specific, plain language (markdown allowed); **always** non-empty. Tie the role to **concrete** facts from the résumé/excerpts; use the **introduction** for voice and motivation **without** verbosity.

## Examples (illustrative; do not copy facts)

**A** — Base résumé fits: resume_needs_update false, skills_section "", projects_section "", cover_letter a tight paragraph linking posting to real strengths.

**B** — Tailor skills/projects: resume_needs_update true, skills_section and/or projects_section filled with the rewritten blocks, links preserved, bullet lengths similar; cover_letter short and specific."""


def materials_system(*, introduction_markdown: str, base_resume_latex: str) -> str:
    """MATERIALS_SYSTEM plus the introduction and base resume: identical for
    every job, so it is sent as the system prompt and cached across calls."""
    intro_block = introduction_markdown.strip() or "(no introduction section in profile)"
    # The page only fits as many projects as the base resume has (measured on
    # the 2026-09-29 run), so tell the model the number instead of hoping
    # "keep it one page" is enough.
    n = resume_splice.count_projects(
        resume_splice.editable_blocks(base_resume_latex)[resume_splice.PROJECTS_SECTION]
    )
    project_cap = f"""

## Project count (hard limit)

The base résumé's page is already full with its {n} projects. Each extra project adds 3-4 lines and pushes the last section onto a second page, which shortening words cannot win back.
- **projects_section must contain exactly {n} project entries** (`\\\\resumeSubItem`), never more, even when more highlighted project excerpts are provided.
- Choose the {n} projects most relevant to this job (from the excerpts or the base), and list the **most relevant first**.""" if n else ""
    from policy import personal

    materials = MATERIALS_SYSTEM.replace("{first_name}", personal.first_name())
    return f"""{materials}{project_cap}

## Candidate introduction (use for cover letter voice and personalization; stay concise)

---
{intro_block}
---

## Base resume (full LaTeX)

---
{base_resume_latex}
---"""


def materials_user_prompt(*, job_bundle: str, project_excerpts: str) -> str:
    excerpts_block = project_excerpts.strip() or "(no project excerpts)"
    return f"""Job opening:

---
{job_bundle}
---

Highlighted project excerpts (markdown):

---
{excerpts_block}
---

Produce JSON only. If resume_needs_update is true, fill skills_section and/or \
projects_section with complete section blocks. If false, leave both empty. Always fill \
cover_letter."""


def resume_fix_user_prompt(
    *,
    job_bundle: str,
    project_excerpts: str,
    skills_section: str,
    projects_section: str,
    problem: str,
) -> str:
    """Retry prompt: hand back the model's own blocks with the problem named,
    so it fixes them instead of regenerating from scratch."""
    return f"""{materials_user_prompt(job_bundle=job_bundle, project_excerpts=project_excerpts)}

Your previous Skills and Projects blocks, spliced into the base resume, failed \
verification: {problem}

Previous skills_section:

---
{skills_section.strip() or "(unchanged base block)"}
---

Previous projects_section:

---
{projects_section.strip() or "(unchanged base block)"}
---

Return corrected skills_section and projects_section only (no cover letter). Keep the \
same structure; fix exactly the problem above."""
