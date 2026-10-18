# jobo

jobo is a personal job-application pipeline. It reads the job-alert emails in
your Gmail, opens each job page, asks an LLM whether the job fits your profile,
tailors your resume and writes a cover letter for the good fits, and then has a
browser agent fill in and submit the application. It is meant to run on its
own twice a day on your own Linux machine.

It is built for one person applying for their own jobs. It is not a hosted
service, a bulk-application tool or a way to apply on someone else's behalf.

> **jobo sends real job applications under your name.** Read
> [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) before you run the apply stage.

## How it works

```
Gmail job alerts
      │  module1  collect  (Gmail API, read-only use of your mail)
      ▼
data/jobs.db  (SQLite, one row per job link)
      │  module2  read     (Playwright + a local Ollama model)
      ▼
structured job fields (title, company, location, description, ...)
      │  module3  sort     (claude CLI, or Hermes)
      ▼
should_apply + tailored resume PDF + cover letter PDF
      │  module4  apply    (Hermes browser agent in jobo's own Chromium)
      ▼
application submitted / skipped / failed, recorded per job
```

A second source, jobfound.org (`sources/jobfound.py`, `jobo collect-jobfound`),
stores jobs that are already enriched from the site's own data, so they skip
module 2. Every source goes through one insert path with cross-source dedup,
and rule filters (`policy/job_filters.py`, `jobo prefilter`) drop unwanted jobs
before triage. See [docs/sources.md](docs/sources.md).

Every stage reads its input from `data/jobs.db` and writes its result back, and
every stage skips work that is already done, so a run can be stopped and started
again without doing anything twice.

### The stages

**1. Collect (`module1/`, `jobo collector`).** Signs in to Gmail with an OAuth
client from your own Google Cloud project, lists recent mail (`--hours`, 7 days
by default), parses job-alert emails (LinkedIn and other boards) and stores each
job link as a row in `data/jobs.db`. Links already in the database are skipped.
The OAuth scope is `gmail.modify`: jobo reads job-alert mail and marks it read;
it never sends or deletes mail.

**2. Read (`module2/`, `jobo enrich`).** Opens each new job page with
Playwright, keeps only the page text that matters (title, company, location,
description) and asks a small local model in [Ollama](https://ollama.com/)
(`OLLAMA_MODEL`; `./setup.sh` sets `qwen2.5:3b`) to turn it into structured JSON.
Each row is tried once; `--force` retries.

**3. Sort (`module3/`, `jobo triage`).** For every enriched job:

- a **fit call** scores the job 0-5 against your profile (`personal/profile.md`).
  A score of 3 or more (`JOBO_MIN_FIT_SCORE`, `--min-score`) sets
  `should_apply`. Hard exclusions in code (`module3/exclusions.py`, e.g.
  internships) stay out whatever the score.
- for jobs worth applying to, a **materials call** rewrites only the Skills and
  Projects sections of your LaTeX resume (`personal/resume.tex`), which are
  spliced back into the original so the rest cannot change, compiles it with
  `latexmk` and checks it is still one page (otherwise the base resume is
  used). It also writes a cover letter, turned into a PDF with `pandoc`.

The LLM is the [Claude Code](https://claude.com/claude-code) CLI (`claude -p`,
default model `sonnet`, `JOBO_CLAUDE_MODEL`), or Hermes with
`JOBO_LLM_BACKEND=hermes`. Before the materials call, a LinkedIn listing that is
already closed is marked closed and gets no materials.

**4. Apply (`module4/`, `jobo apply`).** A
[Hermes](https://github.com/NousResearch/hermes-agent) browser agent opens each
`should_apply` job and fills in the real application form:

- It runs in a dedicated Hermes profile named `jobo`
  (`scripts/setup_hermes_profile.sh`): no persona or memory, no fallback models,
  and the `hermes_plugin/jobo-browser` plugin, which trims page snapshots to
  form controls, headings and status text, keeps the agent on the right tab and
  stops it at bot walls. The apply model must be a Claude Sonnet model
  (`module4/model_guard.py`).
- The Hermes version is pinned (`scripts/pin_hermes.sh`, run through
  `scripts/hermes_pinned.sh`) because Hermes updates itself. Before each apply
  stage `scripts/check_directsdk_title.py` checks that Hermes' Claude provider
  plugin still keeps Claude Code's session-title request from replacing the
  agent's reply (GHO-10; fixed upstream in plugin 0.3.1);
  a browser fix to Hermes itself is carried in `patches/` (see
  [patches/README.md](patches/README.md)).
- The browser is jobo's own Chromium profile (default `$HOME/jobo-linkedin-profile`,
  `JOBO_BROWSER_PROFILE`), signed in to LinkedIn once during setup. The agent
  attaches to it over the Chrome DevTools Protocol.
- With `--form-workers N` (the pipeline uses 3), one worker keeps the
  LinkedIn-signed-in browser, applies to LinkedIn Easy Apply jobs and routes the
  rest; up to 5 form workers, each with its own fresh Chromium profile that never
  signs in to LinkedIn, apply on employers' own sites (`module4/parallel.py`).
- Code, not the model, does the fiddly parts: uploading this job's resume,
  choosing dropdown values (`module4/answer_book.py`), reading the one-time
  codes that application sites email you (`module4/otp_lookup.py`, which reads
  only recent codes from your Gmail), and resetting a job-site password
  when an account already exists. Every account jobo creates uses the one
  password in `personal/secrets.json`.
- Captchas and bot checks are **not** bypassed. A job behind one is skipped and
  recorded (`module4/blockers.py`). Each job gets at most 3 attempts
  (`JOBO_APPLY_MAX_ATTEMPTS`), each with a limited number of agent steps
  (`JOBO_APPLY_MAX_TURNS`).
- After each real submission jobo waits 30 s plus 0-15 s of jitter before the
  next one to the same site (`JOBO_APPLY_DELAY_SEC`, `JOBO_APPLY_JITTER_SEC`).
  Sites that keep failing hard are skipped for the rest of the run
  (`module4/circuit_breaker.py`); ATS sites can be paused by hand
  (`policy/paused_sites.py`, `JOBO_PAUSED_SITES`).

`jobo funnel` reports, for a time window, the share of `should_apply` jobs whose
listing was still open that were actually submitted, and where the others ended
up.

### Where things live

| Path | What |
| ---- | ---- |
| `main.py` | the `jobo` CLI (Click) |
| `db/` | SQLAlchemy model of the `jobs` table and in-place SQLite migrations |
| `module1/` … `module4/` | the four stages above |
| `sources/` | the source seam: `JobCandidate`, URL normalization, the shared insert path with dedup, the jobfound.org collector (`docs/sources.md`) |
| `policy/` | your personal config loader (`personal.py`), employer blocklist, paused sites |
| `hermes_plugin/jobo-browser/` | the Hermes plugin used by the apply agent |
| `jobo_setup/` | `./setup.sh` wizard and `jobo doctor` |
| `scripts/` | pipeline, backup, Hermes helpers and the regression tests |
| `personal.example/` | a complete config for a fictional person (Alex Example) |
| `personal/` | **your** config (gitignored, written by `./setup.sh`) |
| `data/` | `jobs.db`, logs and per-job resumes/cover letters (gitignored) |
| `credentials/` | Gmail OAuth client and token (gitignored) |
| `docs/` | design notes for modules 1-3 |

## Requirements

- **Linux** with a desktop session (the apply browser runs headed and needs
  `DISPLAY`). jobo is developed and tested on Linux only. The pipeline script
  has some macOS handling, but macOS is untested.
- **Python 3.11+** and [uv](https://docs.astral.sh/uv/).
- **Chromium** (`sudo apt install chromium`, or `uv run playwright install chromium`).
- **Ollama** running locally with a small model pulled (`ollama pull qwen2.5:3b`).
- **Claude Code** (`claude` CLI), signed in, for triage. The usage limits below
  read your Claude subscription usage, so jobo expects a Claude subscription
  rather than API billing.
- **Hermes Agent** (`hermes`) signed in to the same Claude account, for the apply
  agent.
- **LaTeX** (`latexmk`, `pdflatex`) for resumes and **pandoc** for cover letters.
- A **Google Cloud project of your own** with the Gmail API enabled and a
  Desktop-app OAuth client. Nothing is shared; `./setup.sh` lists the steps.
- A **LinkedIn account**. Job alerts come from LinkedIn and Easy Apply needs it.
- Optional: the GitHub CLI (`gh`) for the private data backup.

## Setup

```bash
git clone <repo> jobo
cd jobo
./setup.sh
```

`./setup.sh` installs the Python dependencies (`uv sync`) and then walks you
through each step. It is safe to re-run: current values are shown as defaults
and it asks before replacing a file.

1. **`.env` and usage limits**: copies `.env.example` to `.env`, asks for the
   Ollama model and the two usage limits.
2. **Software**: checks uv, Chromium, Ollama (offers to pull the model), the
   `claude` CLI, Hermes (offers to create the `jobo` profile and pin Hermes)
   and LaTeX.
3. **About you**: name, contact details, links, school, city, education,
   current job and work history, pay, notice period, work authorization, job
   preferences, skills and projects. Written to `personal/`.
4. **Resume**: the path to your LaTeX resume (or start from the bundled
   template). It must have `Programming Skills` and `Projects` sections; these
   are the only parts triage tailors. Compiled to `personal/resume.pdf`.
5. **Job-site account password**: the one password jobo uses for job-site
   accounts, stored in `personal/secrets.json` (mode 600).
6. **Gmail**: how to create your own OAuth client, then the path to its JSON
   and a browser sign-in (`auth_manual.py`).
7. **jobo's browser**: creates the browser profile and opens it so you can sign
   in to LinkedIn.
8. **Cron**: optionally adds two crontab lines for the twice-daily run.
9. **Data backup**: optionally sets up a private GitHub repo for `data/jobs.db`.

It ends by running `jobo doctor`.

```console
$ ./setup.sh --help
usage: setup.sh [-h] [--non-interactive] [--answers ANSWERS] [--skip-installs]

Set up jobo on this machine.

options:
  -h, --help         show this help message and exit
  --non-interactive  no prompts; needs --answers
  --answers ANSWERS  JSON answers file (see setup.answers.example.json)
  --skip-installs    only check system software, install nothing
```

**Non-interactive mode** takes all answers from a JSON file. Copy
`setup.answers.example.json` (the fictional Alex Example), edit it, and run:

```console
$ ./setup.sh --non-interactive --answers setup.answers.example.json --skip-installs
...
== Gmail ==
  [TODO] gmail client: no client JSON given
  [SKIPPED] gmail sign-in: needs a real login: uv run python auth_manual.py

== jobo's browser (LinkedIn sign-in) ==
  [WROTE] browser profile: <profile dir>
  [SKIPPED] linkedin sign-in: needs a real login: re-run ./setup.sh interactively
...
== jobo doctor --offline ==
...
```

Steps that need a real login (Gmail, LinkedIn, `gh repo create`) are always
skipped in this mode; run `./setup.sh` interactively once for those. Cron is
only installed when `install_cron` is `true`, and existing files are kept unless
`overwrite` is `true`. Prefer `account_password_env` (the name of an environment
variable holding the password) over writing the password into the file.

### Checking the machine: `jobo doctor`

`jobo doctor` prints one line per requirement with a fix for anything missing.
`--offline` skips the checks that need a real login, and `--json` prints the
same checks as JSON. It exits 0 when nothing is `MISSING`.

```console
$ env -u PYTHONPATH uv run jobo doctor --offline
OK                 python deps           12 modules importable
OK                 personal config       <repo>/personal: candidate.json, candidate_facts.md, profile.md, resume.tex, resume.pdf, secrets.json
WARN               candidate.json        still the fictional example person (alex.example@example.com)
                                         fix: re-run ./setup.sh with your own details
...
SKIP (needs login) gmail token           credentials/token.json not found
                                         fix: uv run python auth_manual.py
OK                 latex                 latexmk, pdflatex
OK                 usage limits          session 30%; weekly 50%
WARN               cron                  no twice-daily run installed (optional)
                                         fix: re-run ./setup.sh and answer yes to cron
...
15 OK, 0 MISSING, 4 WARN, 3 SKIP
offline mode: items that need a real login were skipped, not checked
READY
```

Don't run `jobo triage` or `jobo apply` until `jobo doctor` (without
`--offline`) shows your own details and no `MISSING` items.

`env -u PYTHONPATH` only matters if your shell sets `PYTHONPATH` (some agent
terminals do), which would shadow the project's `.venv`. Elsewhere,
`uv run jobo ...` is enough.

## Usage

```console
$ env -u PYTHONPATH uv run jobo --help
Usage: jobo [OPTIONS] COMMAND [ARGS]...

  Job Application Automation System

Options:
  --help  Show this message and exit.

Commands:
  all          Run collector then enrich, each with its default options.
  apply        Module 4: submit applications for should_apply jobs...
  budget-gate  Weekly Claude budget gate for scripts/daily_pipeline.sh.
  collector    Collect job-alert emails from Gmail and store in database
  doctor       Check this machine is ready to run jobo: one line per...
  enrich       Enrich job rows from LinkedIn pages via Playwright + Ollama.
  funnel       The apply-rate funnel: of should_apply=1 jobs still open,...
  triage       Module 3: triage enriched jobs (Claude Code fit +...
```

`uv run jobo <command> --help` lists each command's options. The ones you will
use most:

| Command | What it does |
| ------- | ------------ |
| `jobo collector [--hours N] [--dry-run]` | read job alerts from the last N hours (default 168) into `data/jobs.db` |
| `jobo collect-jobfound [--dry-run] [--quiet]` | read new jobs from jobfound.org, filter and dedup them, and store them already enriched (see `docs/sources.md`) |
| `jobo enrich [--job-id ID] [--force] [--dry-run]` | read the pages of jobs not read yet |
| `jobo prefilter [--dry-run] [--quiet]` | drop enriched, unscored jobs that fail the rule filters (`policy/job_filters.py`) before triage; no Claude |
| `jobo triage [--job-id ID] [--min-score N] [--queued] [--force] [--dry-run]` | score fit and make resume/cover PDFs; `--queued` redoes only the jobs waiting to be applied to (after you change your resume or profile) |
| `jobo apply [--limit N] [--job-id ID] [--form-workers N]` | **submit real applications** |
| `jobo apply --plan [--job-id ID] [--limit N]` | print how each queued job would be routed (ATS kind, blocklist, paused site, LinkedIn preflight, which worker); opens nothing, writes nothing |
| `jobo apply --form-workers N --canary-only` | start the apply browsers, check the agent can attach to them, and stop: no job is claimed, nothing is applied |
| `jobo funnel [--since DATE] [--until DATE] [--run-log FILE]` | share of `should_apply` jobs actually submitted |
| `jobo budget-gate --stage NAME` | print the current Claude usage reading and whether a run may start (uses no tokens) |
| `jobo doctor [--offline] [--json]` | check the machine |

**`jobo apply --dry-run` is not a dry run.** It still runs the real apply agent
on real forms; it only skips writing the result to the database. Use
`--canary-only` when you want to test the apply setup without applying, and
`--plan` to see where each job would go.

### The daily pipeline

`scripts/daily_pipeline.sh` runs the stages in order: budget check,
`collector --hours 168`, `collect-jobfound` (`JOBO_JOBFOUND_ENABLED=0` turns it
off; a failure there does not stop the run), `enrich`, `prefilter`, budget
check, `triage`, `apply
--form-workers 3` (`JOBO_APPLY_FORM_WORKERS`). It writes a log per run to
`data/logs/run_<YYYYMMDD_HHMM>.log` and one JSON verdict line per run to
`data/logs/pipeline_verdicts.jsonl` (`OK`, `FAILED`, `SKIPPED_BUDGET`,
`STOPPED_BUDGET`, `FAILED_GMAIL_AUTH`, `SKIPPED_OVERLAP`). A lock
(`/tmp/jobo_pipeline.lock`, via `flock`) makes sure only one run happens at a
time; a run that finds the lock taken skips its slot.

The setup wizard's cron step adds these two lines (10:00 and 22:00, machine
local time):

```
0 10 * * * <repo>/scripts/daily_pipeline.sh >> <repo>/data/logs/cron.log 2>&1
0 22 * * * <repo>/scripts/daily_pipeline.sh >> <repo>/data/logs/cron.log 2>&1
```

Only the 2 lines are added; the rest of your crontab is kept. The machine must
be on, logged in to the desktop session (for the headed browser) and online at
those times.

### Backing up your data

`data/` is never committed: it holds every job you looked at and every
application jobo sent. `scripts/backup_data.sh` copies `data/jobs.db` into a
separate **private** GitHub repo and pushes it. It refuses to push unless
`gh repo view` reports the repo as private. The pipeline does not call it; run it
yourself, or from cron after a run.

```console
$ bash scripts/backup_data.sh --help
# Back up jobo's job history (data/jobs.db) to a PRIVATE GitHub repo.
...
#   JOBO_DATA_REPO=you/jobo-data   (in .env or the environment; required)
#   JOBO_DATA_CLONE=<dir>          (optional; default data/.backup-repo)
#
# One-time setup (./setup.sh offers to do this):
#   gh repo create you/jobo-data --private
#   echo 'JOBO_DATA_REPO=you/jobo-data' >> .env
#
# Usage: scripts/backup_data.sh [-m "commit message"] [--dry-run]
...
```

## Configuration

### Your details: `personal/`

Everything about you lives in `personal/` at the repo root. It is gitignored and
`./setup.sh` writes it. `JOBO_PERSONAL_DIR` points jobo at another directory.
`personal.example/` has the same files for a fictional person. If there is no
`personal/`, jobo falls back to the example so tests and `jobo doctor` work, and
`jobo doctor` warns about it. See `policy/personal.py`.

| File | What |
| ---- | ---- |
| `candidate.json` | structured facts the apply code uses directly: name, email, phone, location, LinkedIn/GitHub/website URLs, the file name used for uploads, and other spellings of your school and city (forms spell them differently) |
| `candidate_facts.md` | the "Candidate facts" block the apply agent sees: education, current job, work history, pay, notice period, work authorization, relocation. Free text; the single source of truth for answering form questions |
| `profile.md` | your profile for triage: introduction, what you want next, experience, and a `# Projects` section where each `## Heading` is one project |
| `resume.tex` | your base LaTeX resume. Triage only replaces its `Programming Skills` and `Projects` sections |
| `resume.pdf` | the compiled base resume, uploaded when no tailored one is made |
| `secrets.json` | `{"account_password": "..."}`: the one password jobo uses for job-site accounts. Keep it mode 600. `JOBO_ACCOUNT_PASSWORD` in the environment works too |

Keep these files truthful. The apply agent copies from them into real forms.

### `.env`

`./setup.sh` copies `.env.example` to `.env`. Everything is optional except what
the wizard sets. The main keys:

| Key | Default | What |
| --- | ------- | ---- |
| `OLLAMA_HOST`, `OLLAMA_MODEL` | `http://127.0.0.1:11434`, `qwen2.5:3b` in `.env.example` | the local model used by `enrich` |
| `JOBO_ENRICH_WORKERS` | | parallel Ollama calls in `enrich` |
| `JOBO_LLM_BACKEND` | `claude` | `claude` or `hermes` for triage |
| `JOBO_CLAUDE_MODEL`, `JOBO_CLAUDE_FIT_MODEL` | `sonnet`, same | triage models (the fit call can use a cheaper one) |
| `JOBO_TRIAGE_WORKERS` | | parallel fit calls |
| `JOBO_MIN_FIT_SCORE` | `3` | fit score needed for `should_apply` |
| `JOBO_ARTIFACTS_DIR` | `data/module3/artifacts` | where tailored resumes and cover letters go |
| `JOBO_BROWSER_PROFILE` | `$HOME/jobo-linkedin-profile` | jobo's signed-in Chromium profile |
| `JOBO_APPLY_FORM_WORKERS` | `3` | form workers used by `daily_pipeline.sh` |
| `JOBO_APPLY_MAX_JOBS` | `0` (no cap) | applications per run |
| `JOBO_APPLY_DELAY_SEC`, `JOBO_APPLY_JITTER_SEC` | `30`, `15` | wait after each submission |
| `JOBO_APPLY_MAX_TURNS` | | agent step budget per job |
| `JOBO_HERMES_PROFILE` | `jobo` | Hermes profile for the apply agent |
| `JOBO_PAUSED_SITES` | | comma-separated ATS domains to leave alone for now |
| `JOBO_BLOCKED_EMPLOYERS` | | comma-separated employers never to triage or apply to |
| `JOBO_SESSION_THRESHOLD`, `JOBO_BUDGET_THRESHOLD` | `0.30`, `0.50` | usage limits, below |
| `JOBO_DATA_REPO` | | `owner/name` of the private backup repo |

Gmail credentials are files, not `.env` keys: `credentials/client_secret.json`
(your OAuth client) and `credentials/token.json` (written by `auth_manual.py`).

## Usage limits

triage (`claude -p`) and the apply agent (Hermes) both run on your Claude
subscription, and a full run can use a lot of it. `module4/budget_guard.py`
reads the subscription's current usage (the same numbers the Claude Code UI
shows; reading them spends no tokens) and stops jobo before it eats the rest:

- **Weekly**: no new run starts once 50% of the weekly allowance is used
  (`JOBO_BUDGET_THRESHOLD`).
- **5-hour session**: no new run starts, and `jobo apply` stops claiming new
  jobs, once 30% of the current 5-hour session is used
  (`JOBO_SESSION_THRESHOLD`). With `JOBO_SESSION_PAUSE=1` apply waits for the
  session to reset instead of stopping.

`daily_pipeline.sh` checks before the collector (`SKIPPED_BUDGET`) and again
before triage (`STOPPED_BUDGET`), and `jobo apply` checks between jobs. Skipped
runs pick up again by themselves once the window resets. If usage can't be read,
the run goes ahead and the log says so. To check by hand:

```console
$ env -u PYTHONPATH uv run jobo budget-gate --stage check
BUDGET GATE (check): weekly Claude usage 21% < 50% threshold, ... - proceeding. 5h session usage 0% (...); apply pauses claims at 30%.
{"gate": "check", "decision": "PROCEED", "utilization": 0.21, "threshold": 0.5, ...}
```

Exit code 0 means go ahead, 75 means over a limit. `bash
scripts/daily_pipeline.sh --budget-override` (or `JOBO_BUDGET_OVERRIDE=1`) lifts
the weekly limit for that one run. Never put it in cron's environment.

## Tests

```bash
bash scripts/run_regression_tests.sh                 # full suite
bash scripts/run_regression_tests.sh test_setup_wizard test_budget_gate   # some tests
bash scripts/run_regression_tests.sh --list          # list tests and how each is isolated
```

```console
$ bash scripts/run_regression_tests.sh --list
test_answer_book                 offline/read-only
test_budget_escalation           offline/read-only
test_run_bugfixes                offline/read-only
test_upload_stale_node           legacy-browser
test_upload_tab_targeting        self-isolated
...
```

The default tests are offline or use their own throwaway browsers and local test
pages; they never use your signed-in browser and never apply anywhere
(`scripts/run_isolated_regressions.py --help` explains the isolation). A few
opt-in tests that drive the real apply agent against local test pages, such as
`scripts/test_pipeline_controls_e2e.py`, are not part of the suite; their
docstrings say how to run them. The full suite takes about ten minutes.

Before merging a branch, run the merge gate from its worktree (after `git fetch
origin`):

```bash
bash scripts/premerge_check.sh                       # checks origin/main..HEAD
bash scripts/premerge_check.sh --no-new-test-ok      # tooling/docs-only change
```

It exits 0 only if every check passes: commit author and committer identity
with no attribution trailers, gitleaks finds no secrets, the lines the branch
adds have no hits from the local personal-terms list (`.privacy/terms.txt` in
the main checkout, gitignored), nothing is added under `personal/`, `data/`,
`credentials/` or `.privacy/` (and no `.env*`, database, PDF or TeX file), the
full suite is green, and a `scripts/test_*` file is added or changed. It prints
counts and file names, never the matched value.

## Limits and caveats

- **It submits real applications in your name.** Every form it completes goes to
  a real employer. Watch the first runs and check the results.
- **The LLMs make mistakes.** Fit scores can be wrong, a tailored resume can
  stress the wrong thing, and the agent can misread a form field or pick the
  wrong option. Read a sample of the tailored resumes and cover letters in
  `data/module3/artifacts/` before trusting it.
- **Captchas and bot checks are not bypassed.** Jobs behind them are skipped.
  Some sites need a sign-in method jobo can't do (e.g. Google-only sign-in) or
  ask for documents or ID numbers it doesn't have; those are skipped too.
- **Sites change.** LinkedIn, ATS systems and job-alert emails change their
  pages without notice, which can break the collector, the enrich step or the
  apply agent until the code is updated.
- **Hermes and Claude Code change.** jobo pins Hermes and carries patches for
  it; a new Hermes or Claude Code release can still break the apply stage.
- **Your accounts.** jobo creates job-site accounts with one shared password and
  reads one-time codes from your Gmail. Use a password you use nowhere else.
- **One machine, one run at a time.** It needs the machine on and the desktop
  session logged in at the cron times. Linux only, tested on one setup.
- **No warranty.** See [LICENSE](LICENSE).

## License

MIT. See [LICENSE](LICENSE).
