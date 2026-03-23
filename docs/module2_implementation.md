# Module 2 – Job page enrichment (implementation)

**Status:** In design / implementation  
**Last updated:** March 22, 2026

This document describes how Module 2 is intended to work: load job URLs from the database, open listing pages in a browser, extract text (not raw HTML) for the fields we care about, call a local LLM to produce structured JSON, and persist results. It reflects decisions and constraints discussed so far; anything not yet built or validated is called out explicitly.

---

## Purpose

Module 1 stores rows with at least `job_url`, `source_platform`, and timestamps. Module 2 fills in **job metadata** by visiting each URL, normalizing what we need into plain text, and using an LLM to map that text into JSON that updates SQLite (`company_name`, `job_title`, `location`, `date_released`, `metadata_json`, etc.).

Primary target in the first iteration: **LinkedIn** job posting pages. Other platforms can reuse the same pipeline pattern with different extraction steps.

---

## What Module 2 is not

- It does not re-scrape every row on every run.
- It is not the filtering/scoring module (that is separate product scope; naming in the PRD may differ).
- It does not commit to a single extraction strategy forever: LinkedIn DOM and anti-bot behavior will require iteration (retries, selector tweaks, or alternate approaches).

---

## Row selection: which jobs to process

Eligibility is driven by a **single column** on `jobs`:

| Column | Meaning |
|--------|---------|
| `module2_attempted` | `0` / `false`: Module 2 has **not** run on this row yet — **enqueue** for the automatic pipeline (subject to optional filters like platform or `--limit`). |
| | `1` / `true`: Module 2 has **already run** (success, partial success, or failure) — **do not** auto-run again. |

**Rule:** Run Module 2 on **every** stored job link **once** by default, regardless of whether Module 1 left `company_name`, `job_title`, or `metadata_json` empty or partially filled. The page pass may still add or refine fields.

**After each run:** Set `module2_attempted = true` when the attempt finishes (whether extraction and LLM steps succeeded or not), so blocked pages, 404s, and bad parses are not retried forever.

**Optional later:** A CLI flag such as `--force` may clear or ignore `module2_attempted` for manual re-enrichment; not required for the first implementation.

---

## Database schema (`jobs`)

Relevant columns (see `db/models.py` for the source of truth):

| Column | Notes |
|--------|--------|
| `module2_attempted` | `BOOLEAN NOT NULL DEFAULT 0`. Primary gate for Module 2 scheduling. |
| `company_name`, `job_title`, `location`, `date_released`, `metadata_json` | Populated by Module 1 when possible; Module 2 may fill or update after visiting `job_url`. |

**Existing databases:** `init_db()` runs a small migration that adds `module2_attempted` if the column is missing (SQLite `ALTER TABLE ... ADD COLUMN`). New databases get the column from `create_all` via the ORM.

---

## Open idea: skip scraping using email-derived fields

Using **company name, job id, or similar** from Module 1 to skip opening some URLs is an optional optimization. Not finalized; implement only if it proves useful and reliable.

---

## Pipeline (high level)

1. **Query** – Select rows where `module2_attempted` is false (and optional filters: `source_platform`, `--limit`, etc.).
2. **Browser** – Open `job_url` with Playwright (Python). Session handling (persistent Chromium profile, CDP attach to running browser, headless vs headed) is **implementation detail to be tuned**; see “Browser” below.
3. **Extract (LinkedIn-specific)** – Pull only the text (and minimal structure if needed) from regions of the page where title, company, location, and description live. **Do not** send full HTML to the LLM.
4. **Normalize** – Single plain-text bundle (or a few labeled sections as text) suitable for the model context window.
5. **LLM** – Prompt + desired JSON shape; model returns JSON (local **Ollama** first).
6. **Validate** – Parse JSON; optional schema validation (e.g. Pydantic). On failure, retry policy TBD (single retry with “fix JSON” prompt is a common pattern).
7. **Persist** – Map into `company_name`, `job_title`, `location`, `date_released`, `metadata_json`, etc.; set **`module2_attempted = true`**; commit.

---

## Browser (Playwright)

- **Stack:** Playwright for Python is the default tool for opening pages and waiting for SPA content.
- **Session / auth:** LinkedIn often requires a logged-in session. Reusing a real Chromium profile (`launch_persistent_context`) or attaching via **CDP** to an already running browser are the main approaches to try; which works best in this environment is **not decided until tested**.
- **Reliability:** Expect to iterate on waits, selectors, and handling of interstitials (login wall, rate limit, empty body). Document concrete selectors and failure codes in code comments or a short “LinkedIn extraction” subsection once something works.
- **Stealth / anti-detection:** Optional layer (e.g. stealth-oriented tooling). Treat as escalation if plain Playwright + real session is insufficient—not a prerequisite for the first prototype.

---

## LLM (Ollama first)

- **Default:** Call a local model via Ollama with a strict instruction: output **only** JSON matching our target shape (and handle occasional markdown fences in post-processing if the model adds them).
- **Context size:** After text-only extraction, content should stay small; if truncation is needed, truncate with an explicit marker and record that in `metadata_json`.
- **Fallback (documentation only, not implemented until needed):** If local models are consistently inadequate, a remote API (e.g. **Gemini**) can be integrated behind the same “text in → JSON out” interface. No commitment to provider or SDK until the local path is exercised.

---

## JSON shape and DB mapping

- **Target:** One JSON object per job with fields we care about (aligned with DB columns plus extras).
- **Columns:** Map deterministically into `company_name`, `job_title`, `location`, `date_released` where the model returns them; put extended or uncertain fields in `metadata_json` (e.g. raw bullets, seniority, employment type, model id, scrape timestamp, error details on failure).
- **Attempt flag:** Success or failure, set `module2_attempted` when the Module 2 pass is complete for that row.

---

## CLI / integration

- Mirror Module 1: a **Click** subcommand on `main.py` (exact name TBD, e.g. `enrich` or `module2`) that opens a DB session, runs the pipeline for the selected batch, and prints a short summary (processed, succeeded, failed, skipped).
- Options to define during implementation: `--limit`, `--dry-run`, `--force` (re-run even when `module2_attempted` is true).

---

## Failure modes (behavioral)

| Situation | Direction |
|-----------|-----------|
| HTTP error, timeout, login wall | Record minimal details in `metadata_json` if useful; set **`module2_attempted = true`**; do not infinite-loop. |
| Partial JSON from LLM | Retry policy + validation; avoid writing junk into typed columns without clear rules. |
| Page removed / 404 | Set **`module2_attempted = true`**; no automatic repeat unless `--force` (or equivalent) exists. |

Exact fields stored for debugging should be minimal and reviewable (e.g. error code, HTTP status, one-line message).

---

## What remains uncertain (explicit)

- Exact **LinkedIn** selectors and wait strategy until implemented and tested on real pages.
- **Ollama** model choice and whether `format: json` (or equivalent) is used server-side.
- Optional **Gemini** (or other) fallback: only after the local path is understood.
- **Email-derived skip** optimization: depends on Module 1 data quality.

---

## Related code / schema

- ORM model: `db/models.py` – `Job` / `JobModel` (`jobs` table), including `module2_attempted`.
- DB init / migration: `db/database.py` – `init_db()` and `_ensure_jobs_module2_attempted_column()`.
- Module 1 CLI pattern: `main.py` – `collector` command; Module 2 should follow the same session/init style when added.

---

## Changelog

| Date | Change |
|------|--------|
| 2026-03-22 | Initial Module 2 implementation doc from design discussion. |
| 2026-03-22 | Row selection: `module2_attempted` only; schema + migration in repo. |
