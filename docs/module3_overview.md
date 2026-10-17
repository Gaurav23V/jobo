# Module 3 – Fit decision and application materials

**Status:** Product overview **and** technical implementation spec (implementer-facing)  
**Last updated:** March 25, 2026 (paths, backend and layout updated since)

> The triage LLM is now the Claude Code CLI (`claude -p`, `module3/claude_client.py`)
> or Hermes (`JOBO_LLM_BACKEND=hermes`, `module3/hermes_client.py`), chosen in
> `module3/llm.py`. The sections that describe Gemini (**Why Gemini**,
> **External references**, **Gemini call pattern**) are the original plan and
> are kept for history. The [README](../README.md) describes how jobo works today.

---

## Purpose

After Module 1 (collect jobs from email) and Module 2 (enrich each job from its posting page into structured `metadata_json` and core columns), the database holds enough information about a listing to judge whether pursuing it makes sense.

**Module 3** (CLI: **`jobo triage`**, same idea as `collector` / `enrich`—short verb, unique command name) does two things:

1. **Decide** whether to apply to each eligible job, and persist that decision so Module 4 can skip bad fits entirely.
2. **When the decision is “apply,”** produce the artifacts Module 4 needs: paths to a resume PDF (default or tailored) and a personalized cover letter PDF, stored locally and referenced from the database.

Separating “decide” from “generate materials” keeps costs predictable (no resume work for rejects), keeps reasoning inspectable for you as a human, and gives Module 4 a simple contract: read flags and file paths, then automate submission.

---

## Prerequisites in the pipeline

- **Module 1** has created or updated job rows (at minimum `job_url` and whatever email parsing could fill).
- **Module 2** has **successfully** enriched the row: in the codebase, **`module2_enriched_at` is non-null** and `metadata_json` holds posting-derived content from the LLM (`job_metadata`).

Module 3 **only** processes rows that satisfy that enrichment-success criterion. The pipeline is assumed not to rely on “enrichment failed but email-only metadata remains” as an input shape.

---

## User-maintained context (you provide this)

Module 3 needs a **single source of truth** about you: introduction, experience, preferences, and **projects** the model may recommend highlighting.

### Location and filename

- **Directory:** `personal/` at the repo root (gitignored; `JOBO_PERSONAL_DIR` points elsewhere). See `policy/personal.py`.
- **Default file:** `personal/profile.md` (`JOBO_PROFILE_CONTEXT_PATH` overrides it when it names a file that exists).

`./setup.sh` writes **`profile.md`** from your answers; you can edit it afterwards. `personal.example/profile.md` shows the structure for a fictional person.

### Required content (initial convention)

The file is **Markdown**. Structure it so both **humans** and **code** can rely on it:

1. **Introduction & preferences** – Freeform sections at the top (e.g. `# Introduction`, `# What I want next`), any headings you like. This block is passed to the model as “profile context” in full or as a bounded prefix if size limits require truncation (implementation should log truncation).

2. **Experience** – Same: any headings/body you want included in “profile context.”

3. **Projects** – Each project MUST be a **level-2 heading** whose title is the **canonical project name** used in LLM outputs:

   ```markdown
   ## Exact Project Name Here

   - **GitHub:** https://github.com/...
   - **Stack:** Python, FastAPI, ...
   - **Description:** ...
   - **Links:** ...
   ```

**Rules for implementers parsing this file:**

- Split the document on lines matching `^## ` (regex: `^##\s+(.+)$`).
- The first `##` block might be “Projects” section header—**recommended:** use a single `## Projects` section and nest each project as `### Subheading` **or** use only `## ProjectName` for each project with no ambiguous duplicate names.
- **Simplest convention:** No `## Projects` wrapper; use **only** `## <Unique Project Name>` for each project, after intro/experience sections that use `#` top-level headings only (so every `##` is a project). If you prefer a wrapper `## Projects`, then use `### Project Name` for each project and teach the parser to only collect `###` under `## Projects`.

**Recommendation in code:** Support the **simplest** convention first: everything after the first line matching `^## ` that is **not** in an allowlist of section titles could be treated as projects—or require a marker line `<!-- module3:projects-start -->` before project `##` blocks. The implementation spec should pick **one** rule and document it in the example profile.

**Rule as implemented** (`module3/context_loader.py`, documented in `personal.example/profile.md`): every `## Heading` under `# Projects` is one project; use `#` only for the non-project sections.

**Stability:** Project names in the first LLM response must **exactly** match a parsed project heading (after strip); implement **trim + casefold optional match** only if you document it. Prefer exact match.

### Format changes later

Markdown is the default. If parsing becomes painful, you can introduce TOML/YAML metadata or one-file-per-project **without** changing the high-level Module 3 flow—only `context_loader.py` and this section need updates.

---

## Persisted decision: `should_apply`

The database needs a **nullable boolean** (column name suggested: `should_apply`) distinct from `applied` (submission already happened in Module 4).

| Value   | Meaning |
| ------- | ------- |
| `NULL`  | The **fit (first) phase** has not completed successfully yet—no durable fit outcome for this row. |
| `false` | Fit phase completed; decided not to apply; Module 4 skips. Paths stay `NULL`. |
| `true`  | Fit phase completed; apply. **Materials (second) phase** must finish so both PDF path columns are set (unless you intentionally allow partial state after a crash—see idempotency below). |

**No `module3_attempted` column:** Progress is inferred from **which columns are already filled**, not from a separate attempt flag (see **Idempotency and when to persist**).

**Score vs `should_apply` (threshold):** The first LLM returns an integer **fit score** (e.g. 0–5). Code maps it to `should_apply`, e.g. **score ≥ 3 ⇒ true**, **score < 3 ⇒ false**. The **threshold** (here `3`) is a **Python constant** (e.g. `module3/constants.py`); optional CLI flag `--min-score` can override for experiments.

**Reasoning:** Stored as plain text for human review only; **no** structured parsing for branching.

---

## Idempotency and when to persist

Module 3 is **two phases**. Persist to the DB **as soon as each phase succeeds** so a rerun can **skip redundant API calls** and resume cheaply after failures.

### Phase 1 – Fit API

**Write immediately after a successful first API call and parse** (single transaction):

- `module3_fit_score`
- `module3_fit_reasoning` (may be empty string if the model returns empty—still persist)
- `module3_highlighted_projects` (JSON array text)
- `should_apply` (derived from score and threshold)

**Skip phase 1 on later runs** when `module3_fit_score IS NOT NULL` (treat a non-null fit score as proof phase 1 completed and was committed). Do **not** call the fit API again for that row unless **`--force`** is used.

### Phase 2 – Materials API + PDFs (only if `should_apply` is true)

After the materials LLM response and successful PDF generation (or default resume path resolution), **commit**:

- `module3_resume_pdf_path`
- `module3_cover_pdf_path`

**Skip phase 2 on later runs** when `should_apply IS true` **and** both `module3_resume_pdf_path` and `module3_cover_pdf_path` are non-null and non-empty strings. Do **not** call the materials API or recompile PDFs unless **`--force`** is used.

### Row states (without `--force`)

| State | Phase 1 | Phase 2 |
| ----- | ------- | ------- |
| Never ran Module 3 | Run fit API → persist | If `should_apply` false, stop; if true, run materials → PDFs → persist paths |
| Crashed after fit, `should_apply` true, paths missing | Skip (fit already in DB) | Run materials → PDFs → persist paths |
| Crashed after fit, `should_apply` false | Skip | Skip (nothing to generate) |
| Fully done (`should_apply` false **or** both paths set when true) | Skip | Skip |

### `--force`

Re-run **both** phases for selected rows: ignore the skip rules above, re-call Gemini as needed, regenerate PDFs, and overwrite the Module 3 columns for those rows (implementation may clear paths before regenerate for clarity).

---

## First API call – fit

**Inputs:** A **compact profile** (`context_loader.build_fit_profile`: everything before `# Projects` verbatim, then one line per project: name, stack, first sentence) sent as the cached system prompt, plus a **job bundle** built from the row: `company_name`, `job_title`, `location`, `job_url`, `source_platform`, and `metadata_json` (pretty-printed JSON string of the enriched object).

**Outputs (logical):**

1. **Fit score** – Integer in 0–5 (schema-enforced).
2. **Reasoning** – Freeform string.
3. **Highlighted projects** – Names from the project index, which are the `##` section names in `profile.md`. Use a **JSON array of strings** in the API schema (`highlighted_project_names`); storing in SQLite can be `json.dumps` of that list (no need for comma-separated strings in code).

---

## Second API call – resume and cover (only if `should_apply` is true)

**Inputs:** Job bundle (as above), **base resume LaTeX** (full file from disk, in the cached system prompt with the introduction), **resolved project excerpts** (Markdown or plain text blocks concatenated for only the highlighted projects), and **hard constraints** in the system prompt (e.g. do not change education dates, employers, or fabricated facts; only adjust project/skill emphasis and ordering as instructed).

**Outputs (logical):**

- `resume_needs_update` – Boolean.
- `skills_section` / `projects_section` – The complete `\section{Programming Skills}` / `\section{Projects}` blocks when tailoring; **empty string** keeps the base block.
- `cover_letter` – Plain text or Markdown suitable for PDF conversion.

**Policy:** If both sections are empty, use the **default** resume PDF path. Otherwise `resume_splice.splice` pastes them into the base LaTeX (so heading, Education, Experience and Achievements cannot change), compiles it and checks it is one page. On a problem (two pages, compile error, malformed block) the model gets **one** retry with its own blocks and the problem named; if that fails too, the default resume is used.

Before this call, `module4/preflight.py` checks whether the LinkedIn listing is already closed; a provably closed job is marked closed and gets no materials.

**LLM backend:** `module3/llm.py` sends both calls to Claude Code (`claude -p` with tools, MCP, settings and slash commands off, `--json-schema` for the reply). Set `JOBO_LLM_BACKEND=hermes` to use the old `hermes chat` path. The fit phase runs one call first so the shared system prompt is cached before the parallel calls start.

---

## Local artifacts and database paths

- **`module3_resume_pdf_path`** – Filesystem path to the resume PDF Module 4 should upload (`NULL` when `should_apply` is false).
- **`module3_cover_pdf_path`** – Path to cover letter PDF (`NULL` when `should_apply` is false).

Paths should be stored **relative to a configurable root** (e.g. `JOBO_ARTIFACTS_DIR`) when possible so the repo can move machines; resolve to absolute paths at runtime when calling external tools.

**LaTeX → PDF:** Use `latexmk` or `pdflatex` via `subprocess`; capture stderr on failure and **log** (no requirement to persist errors in DB).

**Cover → PDF:** Prefer **Pandoc** (`pandoc letter.md -o letter.pdf`) if available; document fallback options (e.g. `weasyprint`, or plain `reportlab`) as optional. The implementation should check the tool exists and fail clearly in logs.

---

## Why Gemini

Module 2 uses a **local** Ollama JSON workflow for extraction. Module 3 uses **Google Gemini** over HTTP for stronger judgment and prose. Official docs (see references below) recommend:

- Python package **`google-genai`** ([PyPI](https://pypi.org/project/google-genai/)).
- Env var **`GEMINI_API_KEY`** picked up automatically by `genai.Client()` when unset explicitly.
- **Structured outputs** via `response_mime_type: "application/json"` and `response_json_schema` derived from **Pydantic** `model_json_schema()`, then `Model.model_validate_json(response.text)`.

**Default model id:** Use a configurable string (env `JOBO_GEMINI_MODEL`), defaulting to a current Flash-class model from the official docs (e.g. `gemini-3-flash-preview` as shown in the [quickstart](https://ai.google.dev/gemini-api/docs/quickstart); **verify** the exact id in [Google AI Studio](https://fabrikam.google.com/) when implementing, as preview names change).

---

## Relationship to other modules

| Module | Role |
| ------ | ---- |
| **1** | Ingest emails → job rows. |
| **2** | Enrich posting → `metadata_json` + columns. |
| **3** | Fit + materials + PDF paths. |
| **4** | Submission automation using `should_apply`, `applied`, paths. |

---

## Resolved design choices

- **No DB provenance** for model id, timestamps, or raw API errors (logs only).
- **Idempotency:** No `module3_attempted`. **Phase 1** skipped when `module3_fit_score IS NOT NULL` (unless `--force`). **Phase 2** skipped when `should_apply` is true and both PDF path columns are set (unless `--force`). **`should_apply` NULL** means phase 1 never completed successfully.
- **Threshold:** Code constant (optional CLI override).

---

# Technical implementation

This section is written so a new contributor (or coding agent) can implement Module 3 without guesswork.

## External references (verify while implementing)

| Topic | Official / canonical URL |
| ----- | ------------------------ |
| Gemini API quickstart (Python `google-genai`, `GEMINI_API_KEY`, `generate_content`) | https://ai.google.dev/gemini-api/docs/quickstart |
| Structured output (JSON Schema + Pydantic) | https://ai.google.dev/gemini-api/docs/structured-output |
| `generateContent` REST reference | https://ai.google.dev/api/generate-content |
| Python `google-genai` on PyPI | https://pypi.org/project/google-genai/ |

**Note:** Google’s docs occasionally rename preview models. Always confirm **`model=`** strings against the current API or AI Studio.

---

## Dependencies

Add to `pyproject.toml` (project already uses Python ≥ 3.11, Click, SQLAlchemy, Pydantic, python-dotenv):

```toml
"google-genai>=1.0.0",
```

(Optional) Document system packages separately: a TeX distribution (`latexmk`, `pdflatex`), and optionally `pandoc` for cover PDFs—they are **not** pip packages.

Update `[tool.setuptools.packages.find]` `include` to add `"module3"`.

---

## Environment variables

| Variable | Required | Purpose |
| -------- | -------- | ------- |
| `JOBO_LLM_BACKEND` | No | `claude` (default, `claude -p`) or `hermes`. |
| `JOBO_CLAUDE_MODEL` | No | Model for triage calls (default `sonnet`). |
| `JOBO_CLAUDE_FIT_MODEL` | No | Optional cheaper model for the fit call only. |
| `JOBO_TRIAGE_WORKERS` | No | Parallel fit calls. |
| `JOBO_MIN_FIT_SCORE` | No | Fit score needed for `should_apply` (default 3). |
| `JOBO_PROFILE_CONTEXT_PATH` | No | Path to profile Markdown (default `personal/profile.md`). |
| `JOBO_BASE_RESUME_TEX` | No | Path to your base `.tex` resume (default `personal/resume.tex`). |
| `JOBO_DEFAULT_RESUME_PDF` | No | Pre-built PDF used when no tailored resume is made (default `personal/resume.pdf`). |
| `JOBO_ARTIFACTS_DIR` | No | Root for per-job outputs (default `data/module3/artifacts`). |

The original plan used `GEMINI_API_KEY` and `JOBO_GEMINI_MODEL`; they are no longer read. See `.env.example` for the full list.

---

## Database schema changes

**Table:** `jobs` (SQLite via SQLAlchemy, same pattern as Module 2 migrations in `db/database.py`).

Add columns (names are suggestions; keep consistent in ORM + migrations):

| Column | Type | Nullable | Notes |
| ------ | ---- | -------- | ----- |
| `should_apply` | BOOLEAN | Yes | Set together with fit columns when phase 1 succeeds; `NULL` only until then. |
| `module3_fit_score` | INTEGER | Yes | 0–5 from first call; **persist as soon as call 1 succeeds** (idempotency key for skipping call 1). |
| `module3_fit_reasoning` | TEXT | Yes | Freeform; persist with phase 1. |
| `module3_highlighted_projects` | TEXT | Yes | JSON array of strings; persist with phase 1. |
| `module3_resume_pdf_path` | TEXT | Yes | Set when `should_apply` true, after phase 2; **persist when resume PDF is ready** (default or custom). |
| `module3_cover_pdf_path` | TEXT | Yes | Set when `should_apply` true; **persist when cover PDF is ready**. |

**Migration style:** Mirror `_ensure_jobs_module2_*` in `db/database.py`: use `inspect(engine)` and `ALTER TABLE ... ADD COLUMN` for each missing column so existing `jobs.db` files upgrade in place.

**ORM:** Extend `JobModel` / `Job` dataclass in `db/models.py` like existing Module 2 fields.

---

## Package layout (`module3/`)

```
module3/
  __init__.py
  constants.py          # FIT_SCORE_MIN_APPLY = 3, default paths, model default
  schema.py             # Pydantic models for Gemini structured outputs + internal DTOs
  context_loader.py     # Read profile.md; extract project blocks by heading rules
  llm.py                # picks the backend: claude_client (default) or hermes_client
  claude_client.py      # `claude -p` with a JSON schema for the reply
  hermes_client.py      # one-shot `hermes chat` (JOBO_LLM_BACKEND=hermes)
  prompts.py            # SYSTEM / USER templates for call 1 and call 2 (strings or functions)
  job_bundle.py         # Build text blob from JobModel + json.dumps(metadata, ensure_ascii=False)
  exclusions.py         # hard exclusions applied whatever the fit score
  resume_splice.py      # paste the tailored Skills/Projects sections into the base LaTeX
  resume_guard.py       # checks on the tailored resume
  query.py              # SQLAlchemy query: eligible jobs (see below)
  persist.py            # Apply updates to JobModel; commit session
  pdf.py                # latex → pdf; cover text/md → pdf; subprocess helpers
  runner.py             # Orchestration loop, logging, dry-run, force

personal/               # gitignored, written by ./setup.sh (see policy/personal.py)
  profile.md            # your profile
  resume.tex            # your base resume
  resume.pdf            # compiled base resume
```

(The table below is the original plan; `gemini_client.py` became `claude_client.py` / `hermes_client.py` behind `llm.py`.)

**Purpose summary**

| File | Responsibility |
| ---- | ---------------- |
| `constants.py` | Tunables (threshold, defaults). |
| `schema.py` | `FitDecisionOutput`, `MaterialsOutput` Pydantic models + `model_json_schema()` for Gemini. |
| `context_loader.py` | Load file; list project names; `get_project_bodies(names: list[str]) -> str` for prompt 2. |
| `gemini_client.py` | `call_structured(model, system, user, response_type: type[BaseModel]) -> BaseModel`. |
| `prompts.py` | All prompt wording centralized. |
| `job_bundle.py` | Deterministic serialization of DB row → prompt-safe text. |
| `query.py` | `list_jobs_for_module3(session, *, force: bool)` |
| `persist.py` | Map outputs to columns; handle dry-run (no commit). |
| `pdf.py` | Write `.tex` to temp dir, run `latexmk`, return path; cover letter PDF. |
| `runner.py` | For each job: fit → persist → if apply: materials → PDFs → persist paths. |

---

## Eligibility query

**Base requirement:** `module2_enriched_at IS NOT NULL`.

**Include a row in a normal run (`force=False`)** when Module 3 still has work left:

- **Phase 1 needed:** `module3_fit_score IS NULL` → run fit API (then persist).
- **Phase 2 needed:** `should_apply IS true` **and** (`module3_resume_pdf_path` is null/empty **or** `module3_cover_pdf_path` is null/empty) → run materials API + PDFs (then persist paths). Do **not** re-run phase 1 if `module3_fit_score IS NOT NULL`.

**`--force`:** Include enriched rows per product choice (e.g. all jobs, or all that ever had Module 3 data). Re-execute phases even when skip conditions would apply; overwrite Module 3 columns and artifacts as documented.

**`--dry-run` (recommended):** Exercise Gemini/PDF logic but **do not** `commit()` (mirror Module 2 `enrich --dry-run`). Idempotency checks may still read current DB state to decide what *would* run.

---

## Gemini call pattern (Python)

Aligned with [Structured output](https://ai.google.dev/gemini-api/docs/structured-output):

```python
from google import genai
from pydantic import BaseModel, Field

client = genai.Client()  # uses GEMINI_API_KEY

class FitDecisionOutput(BaseModel):
    fit_score: int = Field(ge=0, le=5, description="Integer 0-5 job fit.")
    reasoning: str = Field(description="Why good or bad fit; unstructured.")
    highlighted_project_names: list[str] = Field(
        default_factory=list,
        description="Project headings from profile.md to emphasize.",
    )

response = client.models.generate_content(
    model=model_id,
    contents=user_prompt,  # or multi-part if needed
    config={
        "system_instruction": system_prompt,
        "response_mime_type": "application/json",
        "response_json_schema": FitDecisionOutput.model_json_schema(),
    },
)
out = FitDecisionOutput.model_validate_json(response.text)
```

If your installed `google-genai` version does not accept `system_instruction` in `config`, prepend the system text to the user message with a clear delimiter (e.g. `SYSTEM:\n...\n\nUSER:\n...`) and consult the SDK’s `GenerateContentConfig` / release notes.

**Materials call** uses a second Pydantic model, e.g. `MaterialsOutput` with `resume_needs_update: bool`, `resume_latex: str`, `cover_letter: str`.

**Retries:** Optional small retry loop on `ValidationError` or empty `response.text` (similar spirit to `module2/ollama_client.py`). **Persist phase 1 as soon as call 1 succeeds** (commit fit columns + `should_apply`); only then start phase 2. If phase 2 fails after commit of phase 1, a **rerun skips call 1** and retries phase 2 only.

---

## Orchestration (`runner.py`)

For each eligible job (ordered by `id`):

1. **Load** `profile.md` once per process (or per job if files can change mid-run).
2. **Phase 1 – Fit:** If `module3_fit_score IS NOT NULL` and not `force`, **skip** the fit API; use existing `should_apply` and highlighted projects from the row. Otherwise:
   - Build prompts from `prompts.py` + `job_bundle.py` + full profile text.
   - Call Gemini; validate `FitDecisionOutput`; compute `should_apply`.
   - **Commit** `module3_fit_score`, `module3_fit_reasoning`, `module3_highlighted_projects`, `should_apply` (unless dry-run). This makes the run **idempotent** for phase 1.
3. If `should_apply` is **false**: continue (no phase 2; paths stay `NULL`). Row is finished for Module 3.
4. **Phase 2 – Materials:** If `should_apply` is **true** and both PDF path columns are already non-empty and not `force`, **skip** the materials API and PDF steps. Otherwise:
   - Resolve `highlighted_project_names` via `context_loader`; if name missing, log warning and skip that block or abort phase 2 per policy (document choice).
   - Read base `.tex` from `JOBO_BASE_RESUME_TEX`.
   - **Materials call** with structured `MaterialsOutput`.
   - **Resume PDF:** If `resume_latex` non-empty after strip, write under `JOBO_ARTIFACTS_DIR / {job_id} /`, compile with `pdf.py`; set `module3_resume_pdf_path`. Else set path to `JOBO_DEFAULT_RESUME_PDF` (or copy into artifacts—pick one strategy and document).
   - **Cover PDF:** Write `cover.md` or `.txt`, convert with Pandoc (or chosen backend), set `module3_cover_pdf_path`.
   - **Commit** both path columns when both artifacts exist (unless dry-run).

**Concurrency:** Sequential processing is fine initially; add rate limiting or sleep if you hit quota errors (log HTTP status / message).

---

## CLI integration (`main.py`)

Add a Click command consistent with `collector` and `enrich`. **Command name:** `triage` — triage enriched jobs (fit decision + optional resume/cover PDFs).

```text
jobo triage [OPTIONS]
  --dry-run    Full pipeline, no DB writes
  --force      Ignore idempotency; re-run fit and/or materials as implemented
  --quiet      Less logging
  --min-score  Optional; override minimum fit score to set should_apply (if implemented)
```

Wire `init_db()` + `get_session()` like existing commands. The Python package and DB column prefix stay **`module3_*`**; only the user-facing CLI subcommand is **`triage`**.

---

## Logging

Use stdlib `logging`. Log at INFO: job id, URL, `should_apply`, score. Log at WARNING: missing project name, LaTeX compile stderr summary, Gemini API errors. Do not log full API keys or full profile if logs might leave the machine.

---

## Testing (recommended)

- **Unit:** `context_loader` parsing on fixture Markdown; threshold mapping; path resolution.
- **Integration (manual):** `--dry-run` against one job with test API key.
- **PDF:** Smoke test `pdf.py` with minimal `.tex` on developer machine where TeX is installed.

---

## Changelog

| Date | Change |
| ---- | ------ |
| 2026-03-24 | Initial high-level Module 3 overview. |
| 2026-03-25 | Resolved open points: no DB provenance, no `module3_attempted`, eligibility/threshold clarified; fixed markdown typos. |
| 2026-03-25 | Added full technical implementation: layout, schema, Gemini SDK/docs, migrations, CLI, PDF, env vars, context file spec. |
| 2026-03-25 | Repo scaffolding: `module3/__init__.py`, an example profile, `pyproject.toml` package include, `.env.example` Module 3 vars. |
| 2026-03-25 | Two-phase persistence and idempotency: persist after fit API; skip call 1 when fit score present; skip call 2 when paths present; `--force` overrides. |
| 2026-03-25 | CLI subcommand named **`triage`** (`jobo triage`); package/dir remains `module3/`. |
| 2026-03-25 | Initial implementation in repo: `module3/*`, DB columns, `jobo triage`. |
| 2026-09-28 | Token cuts: direct Claude Code backend instead of Hermes, compact fit profile, shared text in the cached system prompt, section-only resume output spliced into the base, one retry instead of two, dead-listing preflight before materials. Tests: `scripts/test_module3_triage.py`. |
| 2026-10-11 | `module3/exclusions.py` keeps internship titles out of the apply queue in code, whatever the fit score. `jobo triage --queued` re-scores and regenerates materials for the apply queue only (use after changing the resume or profile). |
| 2026-10 | Profile and base resume moved to the gitignored `personal/` directory (`policy/personal.py`, `./setup.sh`). |
