# Module 2 – Job page enrichment (implementation)

**Status:** Initial implementation in repo (selectors and LinkedIn behavior still need real-world tuning)  
**Last updated:** March 22, 2026

## Implementation status (shipped)

- **CLI:** `python main.py enrich` or `jobo enrich` after `pip install -e .`. Flags: `--dry-run`, `--force`, `--quiet`.
- **Code:** Package `module2/` — `query.py` (pending rows), `linkedin_fetch.py` (Playwright + text extraction), `ollama_client.py` (HTTP `POST /api/generate` with `format: json` + one retry on bad JSON), `enrichment_schema.py` (Pydantic), `persist.py` (job-only `metadata_json` = LLM `job_metadata`; columns `module2_enriched_at`, `module2_model`, `module2_last_error`; `module2_attempted`), `runner.py` (orchestration).
- **Setup:** Install browsers once: **`playwright install chromium`** (or `playwright install`). Configure `.env` using `.env.example` at repo root (`OLLAMA_*`, `JOBO_PLAYWRIGHT_*`, optional `JOBO_LINKEDIN_*`, optional `JOBO_ENRICH_DELAY_MS_MAX`).
- **Verification:** Manual only — run `collector` then `enrich`; use `--dry-run` to exercise Playwright + Ollama without DB writes.

---

This document describes how Module 2 is intended to work: load job URLs from the database, open listing pages in a browser, extract text (not raw HTML) for the fields we care about, call a local LLM to produce structured JSON, and persist results. It reflects decisions and constraints discussed so far; anything not yet built or validated is called out explicitly.

---

## Purpose

Module 1 stores rows with at least `job_url`, `source_platform`, and timestamps. Module 2 fills in **job metadata** by visiting each URL, normalizing what we need into plain text, and using an LLM to map that text into JSON that updates SQLite (`company_name`, `job_title`, `location`, `date_released`, `metadata_json`, etc.).

The first implementation targets **LinkedIn** job posting pages only (extraction logic is LinkedIn-specific). Each run processes **every** row that still needs Module 2—no `source_platform` filter, no row cap in the query. Other platforms can reuse the same pipeline pattern later with different extraction steps.

**Relationship to Module 3:** Enrichment makes job descriptions and requirements available for a later **fit decision** and resume/cover generation. Filtering, scoring, and “should I apply?” live in **Module 3** (see `docs/module3_overview.md`), not in Module 2.

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
| `module2_attempted` | `0` / `false`: Module 2 has **not** run on this row yet — **enqueue** for the pipeline (all such rows in one run). |
| | `1` / `true`: Module 2 has **already run** (success, partial success, or failure) — **do not** auto-run again. |

**Rule:** Run Module 2 on **every** stored job link **once** by default, regardless of whether Module 1 left `company_name`, `job_title`, or `metadata_json` empty or partially filled. The page pass may still add or refine fields.

**After each run:** Set `module2_attempted = true` when the attempt finishes (whether extraction and LLM steps succeeded or not), so blocked pages, 404s, and bad parses are not retried forever.

**`--force`:** Implemented — re-process **all** rows, ignoring `module2_attempted` (expensive; use after fixing extraction or prompts).

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

1. **Query** – Select every row where `module2_attempted` is false (full pending set; no `source_platform` or limit clause).
2. **Browser** – Open `job_url` with Playwright (Python). Session handling (persistent Chromium profile, CDP attach to running browser, headless vs headed) is **implementation detail to be tuned**; see “Browser” below.
3. **Extract (LinkedIn-specific)** – Pull only the text (and minimal structure if needed) from regions of the page where title, company, location, and description live. **Do not** send full HTML to the LLM.
4. **Normalize** – Single plain-text bundle (or a few labeled sections as text) suitable for the model context window.
5. **LLM** – Prompt + desired JSON shape; model returns JSON (local **Ollama** first).
6. **Validate** – Parse JSON with Pydantic; on failure, **one** retry via Ollama with a short “fix JSON” follow-up.
7. **Persist** – Map into `company_name`, `job_title`, `location`, `date_released`, `metadata_json`, etc.; set **`module2_attempted = true`**; commit.

---

## Browser (Playwright)

- **Stack:** Playwright for Python is the default tool for opening pages and waiting for SPA content.
- **Session / auth:** LinkedIn often requires a logged-in session. Reusing a real Chromium profile (`launch_persistent_context`) or attaching via **CDP** to an already running browser are the main approaches to try; which works best in this environment is **not decided until tested**.
- **Vanilla `launch`:** Uses a fresh profile, so the implementation should support **saved credentials** (e.g. `JOBO_LINKEDIN_EMAIL` and `JOBO_LINKEDIN_PASSWORD` in `.env`) to perform a sign-in flow before opening job URLs. Do not commit secrets; 2FA, CAPTCHA, or policy limits may still prevent unattended login—persistent profile or CDP often remains the more dependable option.
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

- Mirror Module 1: a **Click** subcommand on `main.py` named **`enrich`** that opens a DB session, runs the pipeline over **all** rows with `module2_attempted` false, and prints a short summary (**attempted**, **succeeded**, **failed**, plus error lines when present).
- **`--dry-run`:** Run the **full** pipeline (Playwright, extraction, Ollama, validation) but **do not** write to the database—no column updates, no `module2_attempted` changes, no commits. Log or print what would have been saved. Still uses LinkedIn and Ollama (not a lightweight “preview only” mode).
- **`--force`:** Re-run even when `module2_attempted` is true (re-process all rows).
- **`--quiet`:** Optional; same idea as `collector` (less log noise).

---

## Failure modes (behavioral)

| Situation | Direction |
|-----------|-----------|
| HTTP error, timeout, login wall | Record minimal details in `metadata_json` if useful; set **`module2_attempted = true`**; do not infinite-loop. (With **`--dry-run`**, skip any DB updates.) |
| Partial JSON from LLM | Retry policy + validation; avoid writing junk into typed columns without clear rules. |
| Page removed / 404 | Set **`module2_attempted = true`**; no automatic repeat unless `--force` (or equivalent) exists. |

Exact fields stored for debugging should be minimal and reviewable (e.g. error code, HTTP status, one-line message).

---

## What remains uncertain (explicit)

- Exact **LinkedIn** selectors and wait strategy until implemented and tested on real pages.
- **Ollama** model choice for your machine; the client uses **`format: json`** on `/api/generate` when the server supports it.
- Optional **Gemini** (or other) fallback: only after the local path is understood.
- **Email-derived skip** optimization: depends on Module 1 data quality.

---

## Related code / schema

- ORM model: `db/models.py` – `Job` / `JobModel` (`jobs` table), including `module2_attempted`.
- DB init / migration: `db/database.py` – `init_db()` and `_ensure_jobs_module2_attempted_column()`.
- CLI: `main.py` – `collector` and **`enrich`** (same `init_db()` + `get_session()` pattern).
- Module 2 package: `module2/` (see **Implementation status** above).

---

## Changelog

| Date | Change |
|------|--------|
| 2026-03-22 | Initial Module 2 implementation doc from design discussion. |
| 2026-03-22 | Row selection: `module2_attempted` only; schema + migration in repo. |
| 2026-03-22 | Query processes all pending rows; no `source_platform` / `--limit` filters in spec. |
| 2026-03-22 | Browser doc: vanilla launch + `.env` LinkedIn credentials for sign-in. |
| 2026-03-22 | CLI: command `enrich`; `--dry-run` = full pipeline, no DB writes. |
| 2026-03-22 | Initial `module2` implementation, docs synced to shipped behavior. |
| 2026-03-24 | Clarified Module 2 vs Module 3; linked `docs/module3_overview.md`. |
