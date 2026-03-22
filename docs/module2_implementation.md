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

Only rows that still have **no usable signal** in any of these three fields should be candidates for a scrape + LLM pass:

| Field | Treat as “empty” |
|--------|-------------------|
| `company_name` | SQL `NULL` |
| `job_title` | SQL `NULL` |
| `metadata_json` | `NULL`, or after parse: empty object `{}`, or otherwise “no enrichment payload yet” (exact rule should match how we write successful/failed enrichment; see below) |

**Rule:** If **any** of `company_name`, `job_title`, or `metadata_json` already carries data we can use for filtering, **do not** enqueue that row for Module 2 again.

**Rationale (hypothesis):** If we could not infer missing fields on the first successful extraction pass, a second pass with the same page is unlikely to recover them; avoid wasted requests and rate-limit risk.

**Implementation note:** Today `metadata_json` is `NOT NULL` with default `'{}'` in the schema. “Empty” for selection must be defined in code (e.g. `NULL` OR `'{ }'` OR parsed dict with no keys / only sentinel keys). Align this definition with whatever we store after a failed or partial run.

---

## Optional: “already attempted” flag (schema change)

For cases where the page is gone, blocked, or extraction fails, we still need to **stop retrying forever** without violating the “any of three fields” rule above.

**Idea:** Add a boolean column (name TBD, e.g. `enrichment_attempted` or `module2_attempted`) that means: Module 2 has already tried this URL (success or failure).

- `false` (or `NULL` if we add the column with default): eligible to try, subject to the three-field rule.
- `true`: do not automatically retry unless we add a separate manual “force re-enrich” path later.

Whether this column is required on day one vs. encoded inside `metadata_json` (e.g. `{"_module2": {"attempted": true, ...}}`) is an implementation choice; a dedicated column keeps queries simple.

**Not validated:** Exact column name and migration path until we implement and run migrations.

---

## Open idea: skip scraping using email-derived fields

Using **company name, job id, or similar** from Module 1 (if present in email parsing) to decide whether a URL must be opened at all is an optional optimization. Not finalized; implement only if Module 1 reliably provides those fields for a subset of rows.

---

## Pipeline (high level)

1. **Query** – Select jobs matching the empty-field rule (and not blocked by “already attempted” if we add it).
2. **Browser** – Open `job_url` with Playwright (Python). Session handling (persistent Chromium profile, CDP attach to running browser, headless vs headed) is **implementation detail to be tuned**; see “Browser” below.
3. **Extract (LinkedIn-specific)** – Pull only the text (and minimal structure if needed) from regions of the page where title, company, location, and description live. **Do not** send full HTML to the LLM.
4. **Normalize** – Single plain-text bundle (or a few labeled sections as text) suitable for the model context window.
5. **LLM** – Prompt + desired JSON shape; model returns JSON (local **Ollama** first).
6. **Validate** – Parse JSON; optional schema validation (e.g. Pydantic). On failure, retry policy TBD (single retry with “fix JSON” prompt is a common pattern).
7. **Persist** – Map into `company_name`, `job_title`, `location`, `date_released`, `metadata_json`, etc.; commit; mark attempt complete if using an attempt flag.

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
- **Columns:** Map deterministically into `company_name`, `job_title`, `location`, `date_released` where the model returns them; put extended or uncertain fields in `metadata_json` (e.g. raw bullets, seniority, employment type, model id, scrape timestamp).
- **Schema changes:** The `jobs` table may gain new columns (e.g. attempt flag). Any change should stay backward-compatible with Module 1 rows.

---

## CLI / integration

- Mirror Module 1: a **Click** subcommand on `main.py` (exact name TBD, e.g. `enrich` or `module2`) that opens a DB session, runs the pipeline for the selected batch, and prints a short summary (processed, succeeded, failed, skipped).
- Options to define during implementation: `--limit`, `--dry-run`, `--force` (if we ever support re-processing).

---

## Failure modes (behavioral)

| Situation | Direction |
|-----------|-----------|
| HTTP error, timeout, login wall | Record failure in `metadata_json` and/or set attempt flag; do not infinite-loop. |
| Partial JSON from LLM | Retry policy + validation; avoid writing partial strings into typed columns without review rules. |
| Page removed / 404 | Mark attempted; no repeat unless manual override. |

Exact fields stored for debugging should be minimal and reviewable (e.g. error code, HTTP status, one-line message).

---

## What remains uncertain (explicit)

- Exact **LinkedIn** selectors and wait strategy until implemented and tested on real pages.
- Whether **`metadata_json` alone** is enough to record “failed attempt” without a new column, or whether a **dedicated boolean** is preferable for queries.
- **Ollama** model choice and whether `format: json` (or equivalent) is used server-side.
- Optional **Gemini** (or other) fallback: only after local path is understood.
- **Email-derived skip** optimization: depends on Module 1 data quality.

---

## Related code / schema

- ORM model: `db/models.py` – `Job` / `JobModel` (`jobs` table).
- Module 1 CLI pattern: `main.py` – `collector` command; Module 2 should follow the same session/init style when added.

---

## Changelog

| Date | Change |
|------|--------|
| 2026-03-22 | Initial Module 2 implementation doc from design discussion. |
