# Module 3 – Fit decision and application materials (overview)

**Status:** Design / product overview (implementation details come later)  
**Last updated:** March 24, 2026

---

## Purpose

After Module 1 (collect jobs from email) and Module 2 (enrich each job from its posting page into structured `metadata_json` and core columns), the database holds enough information about a listing to judge whether pursuing it makes sense.

**Module 3** does two things at a high level:

1. **Decide** whether to apply to each eligible job, and persist that decision so Module 4 can skip bad fits entirely.
2. **When the decision is “apply,”** produce the artifacts Module 4 needs: paths to a resume PDF (default or tailored) and a personalized cover letter PDF, stored locally and referenced from the database.

Separating “decide” from “generate materials” keeps costs predictable (no resume work for rejects), keeps reasoning inspectable for you as a human, and gives Module 4 a simple contract: read flags and file paths, then automate submission.

---

## What Module 3 is not (at this stage)

This document stays at **overview** level: goals, flows, inputs/outputs, and manual setup you must do. It does **not** specify SDK calls, exact SQL migrations, or prompt text. Those belong in a later implementation spec after you sign off on this overview.

---

## Prerequisites in the pipeline

- **Module 1** has created or updated job rows (at minimum `job_url` and whatever email parsing could fill).
- **Module 2** has successfully enriched the row when you want a high-quality fit judgment: in the codebase, success is reflected by populated enrichment outcome (e.g. `module2_enriched_at` set) and `metadata_json` holding the posting-derived job content.

Module 3 should **only run fit and materials on jobs that are ready** (typically: enrichment succeeded). Running on thin email-only metadata is possible as a future fallback but is not the default story in this overview.

---

## User-maintained context (manual step)

Module 3 needs a **single source of truth** about you: background, experience, preferences, and **projects** the model may recommend highlighting.

**You will maintain a context file** (planned location: under `module3/context/`, e.g. one Markdown file). It should include:

- A short introduction and how you like to talk about your profile.
- Experience (as much as you want the model to “know” when judging fit).
- **Projects**, each with at least: name, repo or links, tech stack, description, and any other links you care about.

**Why one file:** It is easy for you to edit without touching code, and the pipeline can load it once per run or per job as needed.

**Project names and the first LLM call:** The first API response will include a **comma-separated list of project names** (names as they appear in your context file). Later steps resolve those strings against the context file to pull the right project blocks for the second call. That is appropriate as long as **names are unique and stable** in the file; ambiguous or renamed headings make matching brittle, so keeping names explicit and duplicated nowhere else is important.

---

## Persisted decision: should we apply?

The database needs a **nullable boolean** (conceptually: “should apply”) distinct from **`applied`** (already used in the schema for “submission happened” in a later module). Meaning:

| Value | Meaning |
|--------|--------|
| `NULL` | Module 3 has not run a fit decision for this job yet. |
| `false` | Decided not to apply; Module 4 skips. |
| `true` | Good enough fit to proceed; Module 4 may apply, using the resume/cover paths Module 3 recorded. |

**Why a threshold in code:** The model emits a **numeric fit score** (e.g. 0–5). Code—not the model—maps that to `should_apply` using a fixed rule (e.g. scores below 3 ⇒ do not apply). That keeps automation deterministic and easy to tune without re-prompting.

**Reasoning field:** The first call also asks for **free-form text**: why this is a good or bad fit. It is **for your review only**; no particular structure is required, and **downstream code does not parse it** for branching. Storing it alongside the score is enough.

---

## First API call – fit (profile + job → score, reasoning, project list)

**Inputs (conceptual):** Your context file, plus the job fields the system already has (title, company, location, and the rich `metadata_json` / description from Module 2).

**Outputs (conceptual):**

1. **Fit score** – Numeric (e.g. 0–5), used only to compute `should_apply` in code.
2. **Reasoning** – Plain text for you; unstructured.
3. **Highlighted projects** – Comma-separated **project names** matching the context file, guiding what to emphasize if you apply.

**Why this shape:** Score drives automation; reasoning builds trust and helps you override bad days; project names link the two LLM steps without over-structuring the reasoning field.

---

## Second API call – resume and cover letter (only if `should_apply` is true)

If `should_apply` is **false**, Module 3 does **not** run the second call and does **not** generate tailored resume or cover artifacts for that job.

If `should_apply` is **true**, the second call runs with roughly: the job context, your **base resume in LaTeX**, strict rules about what may change (e.g. projects/skills sections vs fixed education/experience), and the **project material** resolved from the comma-separated names from the first call.

**Outputs (conceptual):**

- **`resume_needs_update` (boolean):** Declares whether the base resume should be tailored for this job. Implementation will treat this together with the LaTeX field as below (no diffing or comparing old vs new LaTeX in code).
- **Resume LaTeX:** If no tailoring is needed, this field is **empty**. If tailoring is needed, the model returns **full** LaTeX for the resume. **Non-empty LaTeX means “this is the customized resume body.”** There is no separate “did the body change?” check beyond that convention.
- **Cover letter:** A concise, personalized draft for this job (format to be fixed in implementation—e.g. plain text or Markdown—then converted to PDF locally).

**Two resume outcomes when applying:**

| Situation | What Module 4 uses |
|-----------|---------------------|
| `should_apply` true and **`resume_needs_update` false** (and empty resume LaTeX) | **Default** resume PDF (your canonical base, compiled once or copied from a known path). |
| `should_apply` true and **`resume_needs_update` true** with **non-empty** resume LaTeX | **Custom** resume: that LaTeX is compiled to PDF and the path stored for Module 4. |

A **custom** resume is produced only when you are applying **and** the model both sets **`resume_needs_update` to true** and supplies the full LaTeX. The prompt should keep the boolean and the empty/non-empty LaTeX field **consistent**; the implementation spec can state how to handle a rare mismatch (e.g. prefer “empty LaTeX ⇒ use default”).

---

## Local artifacts and database paths

Generated files stay **local** for now (e.g. under a configurable data directory). The database gains paths (or equivalent) for:

- **Cover letter PDF** – Always the personalized one for that job when `should_apply` is true.
- **Resume PDF** – Either the default resume or the customized compile output, depending on whether custom LaTeX was returned.

**Why paths in the DB:** Module 4 should not guess filenames; it should open exactly what Module 3 produced for that row.

**LaTeX → PDF:** Implementation will use a standard local TeX toolchain (details later). Failures should be visible in logs and ideally recorded so a row is not silently marked ready for apply.

---

## Why Gemini (overview)

Judgment and writing benefit from a capable remote model; Module 2 already uses a local JSON-oriented path for extraction. Module 3 is expected to use **Google Gemini via API** (exact model name configurable). API keys and quotas are **manual** setup in your environment (e.g. `.env`); the overview doc does not duplicate secret handling.

---

## Relationship to other modules

| Module | Role |
|--------|------|
| **1** | Ingest emails → job rows. |
| **2** | Fetch posting text → structured `metadata_json` + columns (enrichment, not “filtering”). |
| **3** | Fit decision + optional tailored resume + cover letter + PDF paths. |
| **4** | Submission automation using `should_apply`, `applied`, and file paths. |

---

## Open points for a later implementation doc

- Exact column names and JSON blobs for provenance (model id, timestamps, raw errors).
- Eligibility query edge cases (e.g. enrichment failed but email metadata exists).
- Retry/idempotency flags (`module3_attempted`-style) mirroring Module 2.
- Threshold value and whether it is configurable via CLI or config file.

---

## Changelog

| Date | Change |
|------|--------|
| 2026-03-24 | Initial high-level Module 3 overview. |
