# Job Application Automation System – Product Requirements Document

**Version:** 1.0  
**Date:** March 21, 2026  
**Author:** Gaurav Verma  
**Status:** Draft

---

## Executive Summary

Build a local, modular job discovery and application automation system that reads job-alert emails, filters opportunities based on profile and preferences, and automates application workflows across multiple job platforms. The system prioritizes low friction, data reliability, and independent, testable modules.

The product is structured as a pipeline of independent services, each with a clear input/output contract, allowing for iterative development and easy future integration of new sources or workflows.

---

## Problem Statement

### Current Pain Points

1. **Email ingest is unreliable**: Job-alert emails from LinkedIn, Indeed, Glassdoor, and other platforms have different formats and nested multipart structures; extracting all job links and context currently requires manual parsing or incomplete heuristics.

2. **Manual triage is time-consuming**: Each incoming job alert must be manually reviewed to decide if it is worth pursuing; no automated filtering based on profile, experience, or preferences.

3. **Application material is not tailored**: Resume and cover letter variants must be manually created for each application; opportunity to auto-generate tailored materials is lost.

4. **Referral opportunities are not discovered**: After filtering for good fits, identifying internal connections at target companies and drafting referral messages is manual and often skipped.

5. **No tracking or deduplication**: The same job often appears across multiple platforms or in multiple emails; without centralized tracking, time is wasted revisiting the same opportunity.

---

## Goals and Success Metrics

### Primary Goals

1. **Collect job opportunities reliably** from Gmail job-alert emails into a structured, deduplicated local database.
2. **Filter opportunities automatically** based on user profile, preferences, and job characteristics.
3. **Prepare application materials** (resume variants, drafted answers, cover letter) with minimal manual input.
4. **Streamline application submission** by automating form filling, document uploads, and answering common questions.
5. **Maximize referral conversion** by identifying internal connections, drafting outreach, and automating follow-ups.

### Success Metrics (module 1)

- Collector processes 100% of job-alert emails from three platforms (LinkedIn, Indeed, Glassdoor) without user intervention.
- Duplicate jobs are merged into single records.
- Link extraction from multipart emails.
- Database records contain: company, job title, posting URL, source platform, raw message reference.
---

## Scope: module 1 MVP – Job Collection and Storage

### module 1 Objective
Build a deterministic, reliable job collector that:
- Fetches recent job-alert emails from Gmail via API.
- Parses email content to extract job metadata and links.
- Deduplicates and stores records in a local SQLite database.
- Provides a clean data contract for downstream modules (filtering, scoring, application, referral).

### module 1 Deliverables

1. **CLI tool** with flags for time range, verbosity, and force-reprocess.
   - `job-collector --hours 24` → collects emails from past 24 hours

2. **Gmail integration** - Still needs to be tested if it works or not - using OAuth 2.0 and the Gmail API (`users.messages.list` and `users.messages.get`).
   - Fetch messages in format `full` with parsed payload structure.
   - Fall back to `raw` format for MIME parsing when multipart structure is complex.

3. **Email parser** that extracts:
   - Headers: `From`, `Subject`, `Date`, `Message-ID`.
   - Body content: plain text and HTML variants.
   - Links: all `href` values from HTML, with deduplication and redirect unwrapping.
   - Email classification: detect if sender is a known job-board platform (LinkedIn, Indeed, Glassdoor, etc.).

4. **Opportunity extractor** that converts email content into structured job records:
   - Deterministic parsing for known platforms (LinkedIn, Indeed, Glassdoor formats).
   - LLM fallback for unrecognized formats or edge cases.
   - Returns JSON: `{ company, title, location, posting_url, source_platform, received_at, raw_links, ...}`.

5. **Local SQLite database** with schema to store:
   - `messages`: raw Gmail messages with metadata and parsed bodies.
   - `opportunities`: individual job opportunities extracted from messages.
   - `extraction_log`: status and debug info for each message processed.

6. **Deduplication logic** to merge opportunities that refer to the same job posting across multiple platforms or emails.

### module 1 Non-Goals

- **No filtering or scoring**: module 1 does not evaluate fit; it collects everything.
- **No application logic**: module 1 does not submit applications or check status.
- **No LinkedIn integration**: module 1 does not interact with LinkedIn (avoided due to ToS risks).
- **No referral outreach**: module 1 does not send messages or check for connections.
- **No resume tailoring**: module 1 does not modify resumes or create variants.
- **Limited source integration**: module 1 focuses on Gmail; other sources (job boards, RSS, API alerts) are module 2+.

---

## module 2–4 (Single-Line Roadmap)

- **module 2**: Filtering and scoring module – evaluate opportunities against user profile and preferences, output ranked shortlist.
- **module 3**: Application material generation – create resume variants, draft answers, generate cover letters for top-ranked jobs.
- **module 4**: Browser automation for submissions – fill ATS forms, upload files, answer questions, submit applications; include LinkedIn integration for safe, rate-limited manual outreach with system assistance.

---

## Technical Architecture Overview

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                    Job Automation System                     │
└─────────────────────────────────────────────────────────────┘

module 1: COLLECTOR (This Release)
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Gmail API   │─────▶│   Parser     │─────▶│     DB       │
│   Fetcher    │      │   Pipeline   │      │   (SQLite)   │
└──────────────┘      └──────────────┘      └──────────────┘
       ▲                     ▲                      ▲
       │                     │                      │
     OAuth              Deterministic +          Schema:
     v1 API             LLM Fallback        messages, opportunities,
                                            extraction_log

module 2: FILTER & SCORE (Future)
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Opportunity │─────▶│  Scoring &   │─────▶│   Ranked     │
│   Database   │      │  Filtering   │      │   Shortlist  │
└──────────────┘      └──────────────┘      └──────────────┘
                            ▲
                            │
                      User profile + preferences

module 3: MATERIAL GENERATION (Future)
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│   Ranked     │─────▶│  Resume &    │─────▶│  Application │
│   Jobs       │      │  Cover Gen   │      │   Packet     │
└──────────────┘      └──────────────┘      └──────────────┘

module 4: APPLICATION & REFERRAL (Future)
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Application │─────▶│   Browser    │─────▶│  Application │
│   Packet     │      │  Automation  │      │   Status     │
└──────────────┘      └──────────────┘      └──────────────┘
                            │
                            ▼
                   LinkedIn Referral
                   (manual gated)
```

### Technology Stack

- **Language**: Python 3.11+ (uv for dependency management).
- **CLI**: Click or Typer for CLI interface.
- **Gmail**: Google API Python Client (`google-api-python-client`).
- **Database**: SQLite with SQLAlchemy ORM.
- **HTML/MIME parsing**: BeautifulSoup, email standard library, lxml.
- **LLM**: User's small LLM subscription (OpenAI, Claude, or local via Ollama).
- **Testing**: pytest for unit and integration tests.
- **Logging**: Python logging with file output.

---

## Database Schema (module 1)

### Table (Jobs)

```
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_name TEXT NOT NULL,
  job_title TEXT NOT NULL,
  location TEXT,
  applied BOOLEAN NOT NULL DEFAULT 0,
  referred_json TEXT NOT NULL DEFAULT '[]',
  date_released DATETIME,
  job_url TEXT NOT NULL,
  source_platform TEXT,
  first_seen_at DATETIME NOT NULL,
  last_seen_at DATETIME NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(company_name, job_title, job_url)
);
```

---

## CLI Interface (module 1)

### Command: `job-collector`

```bash
jobo collector [OPTIONS]
```

**Options:**
- `--hours INTEGER` (default: 24) – Fetch emails from past N hours.
- `--quiet` (default: Verbose logging to console)
- `--dry-run` (default: False) – Parse and log without writing to DB.

**Example runs:**
```bash
# Collect job emails from past 24 hours
job-collector --hours 24
```

**Output:** Summary table showing messages processed, opportunities extracted, duplicates merged, errors encountered.

---

## Success Criteria (module 1)

1. **Functional**: CLI collects, parses, and stores 100+ emails end-to-end without errors.
2. **Reliability**: Link extraction from multipart emails.
3. **Deduplication**: System identifies and merges duplicate jobs from multiple emails/platforms.
4. **Performance**: Not decided yet, depends on test runs
5. **Debuggability**: Every email has an extraction log; parsing failures are captured with context for manual review.
6. **Reusability**: Database records have clear structure; downstream modules can plug in without re-fetching Gmail.


