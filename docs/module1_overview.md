# Job Application Automation – Phase 1 (Module 1) Overview

**Version:** 1.0  
**Date:** March 21, 2026  
**Status:** Draft

---

## Module 1: Job Collector

### Purpose

Module 1 is a standalone collector service that ingests job-related emails from Gmail, extracts structured job information, and stores canonical job records in a local SQLite database. The output of Module 1 serves as the input layer for all downstream modules (filtering, scoring, application, referral).

### Scope

Module 1 is responsible for exactly three things:

1. **Collect** – Fetch raw job-alert emails from Gmail for a specified time range.
2. **Parse** – Extract structured job metadata from email content.
3. **Store** – Persist unique job records in SQLite with deduplication.

Module 1 is **not** responsible for filtering, scoring, applying, referrals, or browser automation. Those are downstream modules.

---

## Three Sub-Parts of Module 1

### Sub-Part 1: Collector

**What it does:**
- Fetches raw emails from Gmail via OAuth 2.0 for a user-specified time range (e.g., `--hours 24`).
- Retrieves full email payloads including headers, plain-text body, HTML body, and MIME structure.
- Handles authentication, rate limits, pagination, and retries.

**What it produces:**
- A list of raw email objects, each containing: sender, subject, received timestamp, and complete message payload.

**Status:**
- Gmail OAuth needs to be tested to confirm it works reliably for this use case.
- Rate limiting and pagination behavior needs to be validated with real email volumes.

---

### Sub-Part 2: Parser

**What it does:**
- Takes raw email objects from the Collector.
- Extracts key fields: headers (From, Subject, Date), body content (both plain-text and HTML variants), links, and platform classification (LinkedIn, Indeed, Glassdoor, or unknown).
- Identifies individual job opportunities within each email (one email may contain multiple job listings).
- Structures each job into a normalized record with company, title, location, job URL, and other relevant fields.

**Approach:**
- Uses deterministic, platform-specific parsing logic for known job boards (LinkedIn, Indeed, Glassdoor templates).
- Falls back to generic HTML extraction for unknown formats.
- Uses LLM (via small subscription) as a final fallback for edge cases or unparseable emails, with strict JSON output schema.

**What it produces:**
- A list of structured job opportunity records, each ready for storage or downstream processing.

**Status:**
- Deterministic parsers for each platform need to be written and tested against real email samples.
- LLM fallback behavior needs to be validated for cost and accuracy.

---

### Sub-Part 3: Store

**What it does:**
- Takes the list of structured job records from the Parser.
- Deduplicates jobs that appear across multiple emails or platforms (same job from LinkedIn and Indeed, or repeated alerts).
- Inserts or updates canonical job records in SQLite.
- Maintains a single source of truth for each unique job opportunity.

**Database schema:**
- Single `jobs` table with columns: company_name, job_title, location, applied, referred_json, date_released, job_url, source_platform, first_seen_at, last_seen_at, metadata_json, and standard timestamps.
- Unique constraint on (company_name, job_title, job_url).

**What it produces:**
- Deduplicated, canonical job records persisted in SQLite.

**Status:**
- Schema has been finalized for MVP; exact table structure is defined and ready for implementation.
- Deduplication logic needs to be tested with real duplicate scenarios.

---

## Data Flow

```
Input: CLI command with parameters (e.g., --hours 24)
  ▼
┌────────────────────────────────────┐
│  Sub-Part 1: Collector             │
│  Fetch emails from Gmail           │
└────────────────────────────────────┘
  ▼
List of raw emails with full payloads
  ▼
┌────────────────────────────────────┐
│  Sub-Part 2: Parser                │
│  Extract and structure job data    │
└────────────────────────────────────┘
  ▼
List of structured job records
  ▼
┌────────────────────────────────────┐
│  Sub-Part 3: Store                 │
│  Deduplicate and persist in SQLite │
└────────────────────────────────────┘
  ▼
Canonical job records in database
```

---

## CLI Interface

Module 1 is invoked as a command-line tool:

```bash
job-collector --hours <N> [options]
```

**Parameters:**
- `--hours <N>` – Fetch emails from past N hours (required; e.g., 24, 168).
- `--quiet` – Suppress verbose output (optional; default is verbose).
- `--dry-run` – Parse and log without writing to database (optional).

**Output:**
- Summary table showing:
  - Number of emails processed
  - Number of jobs extracted
  - Number of duplicates merged
  - Any errors encountered
  - Path to local SQLite database

---

## Success Criteria for Module 1

Module 1 is considered complete and successful when:

1. **Collector** – Reliably fetches all job-alert emails from Gmail within the specified time range without crashes or missing data.
2. **Parser** – Correctly extracts job metadata (company, title, location, URL) from emails with high accuracy across LinkedIn, Indeed, and Glassdoor formats.
3. **Store** – Deduplicates jobs accurately and stores them in SQLite with a clear, queryable schema.
4. **Performance** – Processes a typical batch of job emails (50–100 messages) end-to-end in acceptable time.
5. **Usability** – CLI is easy to run and provides clear feedback on success or failure.

---

## Known Unknowns / Open Questions

1. **Gmail API reliability** – Does Gmail API reliably parse and return multipart emails with all links intact?
2. **Email parsing accuracy** – How accurately can deterministic parsers extract job data from LinkedIn, Indeed, and Glassdoor email templates?
3. **LLM fallback cost** – How many LLM calls are needed for typical email batches, and does the cost fit within the user's subscription?
4. **Deduplication edge cases** – How well does URL-based and (company, title) based deduplication handle real-world variations in job URLs or naming?
5. **Email volume** – What is the typical volume of job-alert emails, and how does that affect performance and storage?

---

## Assumptions

1. The user has a working Gmail OAuth 2.0 setup (credentials already configured).
2. Gmail remains the primary source for job alerts in Phase 1; other sources (RSS, direct APIs) are future work.
3. Job-alert emails follow recognizable patterns from known platforms (LinkedIn, Indeed, Glassdoor).
4. SQLite is sufficient for local storage; no multi-user or cloud sync is required.
5. A small LLM subscription (OpenAI, Claude, or local) is available for fallback parsing.

---

## Future Modules (Brief Roadmap)

- **Module 2** – **Job page enrichment** (implemented in repo): Visit `job_url`, extract posting text, local LLM → structured `metadata_json` and columns; CLI `jobo enrich`. See `docs/module2_implementation.md`. This prepares full job text; it does **not** decide whether you should apply.
- **Module 3** – **Fit decision and application packet**: User-maintained context file + API LLM (planned: Gemini) → nullable **should-apply** flag, free-form reasoning for human review, comma-separated project names for tailoring; second call only when applying → optional custom resume LaTeX, cover letter, local PDFs and DB paths. See `docs/module3_overview.md`.
- **Module 4** – Application and Referral: Browser automation for submissions; LinkedIn integration for referrals as scoped.

