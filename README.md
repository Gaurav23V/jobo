# Jobo - Job Application Automation System

A modular job application tracking and enrichment system that collects job postings from Gmail and enriches them with additional data from LinkedIn.

## Purpose/Description

Jobo is a comprehensive job application management system designed to:

1. **Module 1 (Collector)**: Automatically fetch job alert emails from Gmail, parse job posting links, and store them in a local database
2. **Module 2 (Enrich)**: Enrich job records by scraping LinkedIn job pages using Playwright and extracting structured data using local LLM (Ollama)

## Tech Stack

- **Python 3.11+** - Programming language
- **Click** - Command-line interface framework
- **SQLAlchemy** - SQL toolkit and ORM
- **Google Gmail API** - Email fetching and parsing
- **Playwright** - Browser automation for LinkedIn scraping
- **BeautifulSoup4 + lxml** - HTML parsing
- **httpx** - HTTP client
- **Pydantic** - Data validation
- **Ollama** - Local LLM for data extraction
- **python-dotenv** - Environment variable management

## Project Structure

```
jobo/
├── main.py                    # CLI entry point with commands
├── pyproject.toml             # Project configuration and dependencies
├── db/
│   ├── database.py           # Database connection and session management
│   └── models.py              # SQLAlchemy models (JobModel) and dataclasses
├── module1/
│   ├── collector.py          # Gmail API integration for fetching emails
│   ├── parser.py            # Email content parsing logic
│   ├── runner.py            # Module 1 orchestration
│   └── store.py             # Database storage operations
├── module2/
│   ├── linkedin_fetch.py    # Playwright-based LinkedIn scraper
│   ├── ollama_client.py     # Ollama LLM API client
│   ├── enrichment_schema.py  # Pydantic schemas for enrichment
│   ├── persist.py           # Database persistence for enriched data
│   ├── query.py              # Database queries
│   └── runner.py             # Module 2 orchestration
├── credentials/
│   ├── client_secret.json    # Google OAuth credentials
│   └── token.json            # OAuth token (auto-generated)
├── data/
│   ├── jobs.db              # SQLite database
│   └── email_samples.json   # Sample email data
└── docs/                    # Additional documentation
```

## Installation

### Prerequisites
- Python 3.11+
- Ollama (for local LLM inference)
- Google Cloud project with Gmail API enabled

### Setup

```bash
# Clone the repository
git clone <repo-url>
cd jobo

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies using uv (recommended)
pip install uv
uv sync

# Or install using pip
pip install -e .

# Install Playwright browsers
playwright install
```

### Environment Variables

Create a `.env` file or set environment variables:

```env
# LinkedIn credentials (optional, for authenticated scraping)
JOBO_LINKEDIN_EMAIL=your_email@example.com
JOBO_LINKEDIN_PASSWORD=your_password
```

### Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Gmail API
4. Create OAuth 2.0 credentials
5. Download credentials as `credentials/client_secret.json`
6. Run the collector command once to complete OAuth flow

## Usage

### CLI Commands

```bash
# Collect job emails from Gmail (last 24 hours)
jobo collector

# Collect from past N hours
jobo collector --hours 48

# Dry run (parse without writing to DB)
jobo collector --dry-run

# Enrich jobs with LinkedIn data
jobo enrich

# Enrich with force re-processing
jobo enrich --force

# Run full pipeline (collector + enrich)
jobo all
```

### Options

**Collector Command:**
- `--hours`: Fetch emails from past N hours (default: 24)
- `--quiet`: Suppress verbose output
- `--dry-run`: Parse and log without writing to DB

**Enrich Command:**
- `--dry-run`: Full pipeline without writing to DB
- `--force`: Re-process all jobs (ignore module2_attempted flag)
- `--quiet`: Suppress verbose output

## Data Model

### Job Record

| Field | Type | Description |
|-------|------|-------------|
| id | Integer | Auto-generated primary key |
| job_url | String | URL to job posting |
| company_name | String | Company name |
| job_title | String | Job title |
| location | String | Job location |
| applied | Boolean | Application status |
| date_released | DateTime | Job posting date |
| source_platform | String | Platform (e.g., LinkedIn, Indeed) |
| first_seen_at | DateTime | First discovery timestamp |
| last_seen_at | DateTime | Last update timestamp |
| metadata_json | Text | Additional metadata |
| module2_attempted | Boolean | Whether enrichment was attempted |
| module2_enriched_at | DateTime | Enrichment timestamp |

## Architecture

### Module 1: Email Collection Pipeline
1. OAuth authentication with Gmail API
2. Fetch emails matching search criteria
3. Parse email content to extract job links
4. Store jobs in SQLite database

### Module 2: Job Enrichment Pipeline
1. Query jobs that haven't been enriched
2. Launch Playwright browser (with optional LinkedIn login)
3. Scrape job page content
4. Send content to Ollama LLM for structured extraction
5. Update job records with enriched data

## Features

- Rate-limited Gmail API calls with exponential backoff
- Pagination support for large email volumes
- HTML and plain text email parsing
- LinkedIn scraping with JavaScript-rendered content support
- Local LLM integration for privacy-preserving data extraction
- SQLite database with SQLAlchemy ORM
- Comprehensive error handling and logging