<!-- Test fixture for scripts/test_module3_triage.py: a fictional person's
profile, large enough that the compact fit profile is under a third of it. -->

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

### Example Corp - Backend Engineer (Jun 2023 - present, Springfield, onsite)
- Python/PostgreSQL order service, 2M requests a day; p95 latency 900 ms to 120 ms.
- Event-driven reporting pipeline on SQS and Lambda.

### Sample Labs - Software Engineering Intern (Jan 2023 - May 2023, remote)
- Go CLI that replaced the release team's shell scripts.

# Skills

- **Languages:** Python, Go, TypeScript, SQL
- **Backend & APIs:** FastAPI, REST
- **Data & Infra:** PostgreSQL, Redis, Docker
- **Cloud:** AWS (SQS, Lambda, S3), Git, Linux

# Projects

## relay-service
- **GitHub:** https://github.com/example/relay-service
- **Stack:** Go, PostgreSQL, Redis
- **Description:** Delivers signed webhooks to customer endpoints with retries, exponential backoff and a dead-letter queue. Each delivery is idempotent by event id, so a retried batch never double-sends. A per-endpoint circuit breaker stops a failing customer from slowing everyone else, and an admin page replays any event by id. Delivers signed webhooks to customer endpoints with retries, exponential backoff and a dead-letter queue. Each delivery is idempotent by event id, so a retried batch never double-sends. A per-endpoint circuit breaker stops a failing customer from slowing everyone else, and an admin page replays any event by id.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## trail-planner
- **GitHub:** https://github.com/example/trail-planner
- **Stack:** Python, FastAPI, Redis, React
- **Description:** Suggests hiking trails from elevation and weather data. Tile lookups are cached in Redis, which cut page loads from three seconds to about four hundred milliseconds, and the planner explains every suggestion with the elevation profile and the forecast it used. Suggests hiking trails from elevation and weather data. Tile lookups are cached in Redis, which cut page loads from three seconds to about four hundred milliseconds, and the planner explains every suggestion with the elevation profile and the forecast it used.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## budget-bot
- **GitHub:** https://github.com/example/budget-bot
- **Stack:** Python, SQLite, a small local LLM
- **Description:** Chat bot that categorises bank transactions with a small local model, sends a monthly summary and raises a spending alert per category. Categories are learned from corrections, so the same merchant is never asked about twice. Chat bot that categorises bank transactions with a small local model, sends a monthly summary and raises a spending alert per category. Categories are learned from corrections, so the same merchant is never asked about twice.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## log-sifter
- **GitHub:** https://github.com/example/log-sifter
- **Stack:** Rust, ClickHouse
- **Description:** Command-line tool that streams application logs into ClickHouse and groups them by normalised message template, so a noisy service's top ten error shapes show up in one query instead of a day of grepping. Command-line tool that streams application logs into ClickHouse and groups them by normalised message template, so a noisy service's top ten error shapes show up in one query instead of a day of grepping.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## queue-bench
- **GitHub:** https://github.com/example/queue-bench
- **Stack:** Go, SQS, Kafka, Grafana
- **Description:** Benchmark harness comparing SQS and Kafka for a fan-out workload: throughput, tail latency and cost per million messages, with the dashboards and the scripts to reproduce every number. Benchmark harness comparing SQS and Kafka for a fan-out workload: throughput, tail latency and cost per million messages, with the dashboards and the scripts to reproduce every number.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## photo-dedupe
- **GitHub:** https://github.com/example/photo-dedupe
- **Stack:** Python, OpenCV, SQLite
- **Description:** Finds near-duplicate photos in a large library with perceptual hashes and a small nearest-neighbour index, then lets you keep the sharpest copy of each group from a simple web page. Finds near-duplicate photos in a large library with perceptual hashes and a small nearest-neighbour index, then lets you keep the sharpest copy of each group from a simple web page.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## shift-scheduler
- **GitHub:** https://github.com/example/shift-scheduler
- **Stack:** TypeScript, Next.js, PostgreSQL
- **Description:** Scheduling app for a small clinic: staff set availability, the solver builds a fair weekly rota under hard constraints, and swaps go through an approval flow with a full history. Scheduling app for a small clinic: staff set availability, the solver builds a fair weekly rota under hard constraints, and swaps go through an approval flow with a full history.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.

## api-gateway-lite
- **GitHub:** https://github.com/example/api-gateway-lite
- **Stack:** Go, Envoy, OpenTelemetry
- **Description:** A small API gateway with token auth, per-key rate limits and request tracing, built to learn how the pieces of a production gateway fit together and where the latency goes. A small API gateway with token auth, per-key rate limits and request tracing, built to learn how the pieces of a production gateway fit together and where the latency goes.
- **Highlights:** Wrote the design doc, the load tests and the runbook; kept p99 latency within budget through two traffic spikes; added structured logs, metrics and alerts so on-call could see a problem before a user reported it; documented every trade-off so the next engineer could change it safely; reviewed and merged contributions from three other people; kept the test suite under a minute so it ran on every push; and wrote a short postmortem for the one outage, with the fix and the alert that would have caught it earlier.
- **Notes:** Open source under MIT; the README has a five-minute quick start and an architecture sketch.
