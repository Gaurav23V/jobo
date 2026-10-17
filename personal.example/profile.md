<!--
Example profile for a fictional person. ./setup.sh writes your own copy to
personal/profile.md (gitignored).

Parser rule (module3/context_loader.py): every `## Heading` under `# Projects`
is one project; the heading text must match `highlighted_project_names` from
the fit call. Use `#` only for non-project sections (intro, experience, skills).
-->

# Introduction

Hi, I'm Alex Example, a backend engineer in Springfield, USA. I finished a
B.S. in Computer Science at Lakeside Institute of Information Technology, Springfield (Aug 2019 -
May 2023) and have worked on backend services and data pipelines since.

**What I want next**

A backend or platform role where I own services end to end: design, build,
run and fix them. I like measurable work (latency, cost, reliability) and
small teams that ship often. I'm employed, so a move has to be a clear step up
in scope, team or pay.

# Experience

**For anyone evaluating job fit (including automated triage):** I have about
three years of professional backend experience. Do not score a job as a poor
fit only because it asks for up to five years.

**Hard exclusions (score these 0 or 1, whatever the stack match):**

- **Internships of any kind.** I am a graduate in a full-time role; any posting titled or described as an internship, intern, or a stipend-paid position is out.
- **Full-time roles whose stated pay is below 9 LPA** (or the equivalent in another currency, e.g. 90,000 USD a year). If a posting states a range, judge it by its top end. A missing salary is fine.

## Example Corp - Backend Engineer (Jun 2023 - present, Springfield, onsite)
- Python/PostgreSQL order service, 2M requests a day; p95 latency 900 ms to 120 ms.
- Event-driven reporting pipeline on SQS and Lambda.

## Sample Labs - Software Engineering Intern (Jan 2023 - May 2023, remote)
- Go CLI that replaced the release team's shell scripts.

# Skills

- **Languages:** Python, Go, TypeScript, SQL
- **Backend & APIs:** FastAPI, REST
- **Data & Infra:** PostgreSQL, Redis, Docker
- **Cloud:** AWS (SQS, Lambda, S3), Git, Linux

# Projects

## Trail Planner
- **GitHub:** https://github.com/example/trail-planner
- **Stack:** Python, FastAPI, Redis, React
- **Description:** Suggests hiking trails from elevation and weather data. Cached tile lookups cut page loads from 3 s to 400 ms.

## Budget Bot
- **GitHub:** https://github.com/example/budget-bot
- **Stack:** Python, SQLite, a small local LLM
- **Description:** Chat bot that categorises bank transactions, with a monthly summary and per-category spending alerts.
