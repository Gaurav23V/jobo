# Job sources

jobo reads jobs from more than one place. Each place is a **source**. Every
source turns what it reads into the same normalized `JobCandidate` objects and
stores them through one insert path, which also deduplicates across sources.
After that, all jobs go through the same pipeline: enrich, prefilter, triage,
apply.

Today there are two sources:

| Source | Code | `source_platform` | Enriched by |
| --- | --- | --- | --- |
| LinkedIn job-alert emails | `module1/` (`jobo collector`) | `linkedin` | module 2 (browser + Ollama) |
| jobfound.org | `sources/jobfound.py` (`jobo collect-jobfound`) | `jobfound` | the source itself (jobfound's own data) |

## Architecture

```
alert emails ──module1──┐
                        ├─ JobCandidate ─> sources/store.py: insert_candidates
jobfound.org ─sources/──┘   (+ collection-time        (cross-source dedup, one insert path)
             jobfound.py     rule filters)                      │
                                                                ▼
                                                       data/jobs.db (jobs)
                                                                │
          module2 enrich (bare links only; pre-enriched rows are skipped)
                                                                │
          jobo prefilter (policy/job_filters.py, every source, no Claude)
                                                                │
          module3 triage (Claude; skips prefiltered rows) ──> module4 apply
```

The seam has four parts:

- `sources/base.py`: `JobCandidate`, which every source yields. A candidate is
  either a **bare link** (only `job_url` is known; module 2 enriches it later)
  or **pre-enriched** (`enrichment` holds the module 2 `job_metadata` shape and
  `enrichment_model` names the source, so the row is stored already enriched
  and the module 2 scraper never opens it).
- `sources/urls.py`: URL normalization shared by every source and by dedup.
  LinkedIn job links in any shape become `https://www.linkedin.com/jobs/view/<id>/`.
  Other links keep their path and the query parameters the ATS needs. Only
  tracking parameters are removed (`source=<job board>`, `utm_*`, `gclid`, ...).
- `sources/store.py`: `insert_candidates`, the one insert path. See "Dedup" below.
- `policy/job_filters.py`: the rule filters and every tunable constant.

### `metadata_json` keys

Enriched rows keep the job facts module 3 reads (`employment_type`,
`responsibilities`, `requirements`, `description_full`, ...). Keys that start
with `_` are jobo bookkeeping:

- `_sources`: every source this job was seen in (the dedup seen-in list).
- `_facts`: structured filter inputs (`job_type`, `categories`, `salary`,
  `experience`). A source that does not know a value leaves it empty, and the
  matching rule is skipped.
- `_jobfound`: jobfound provenance: id, job page URL, raw category labels,
  published time, and whether the job came from the feed or its page.

## Dedup

`insert_candidates` checks each candidate against the database, and against
earlier candidates in the same batch, in this order:

1. the same normalized `job_url`;
2. the same LinkedIn numeric job id, however the URL is written;
3. the same normalized company and title, first seen within
   `DEDUP_WINDOW_DAYS` (30) days. Two different LinkedIn ids are never merged
   this way.

A duplicate is never inserted. The existing row gets a new `last_seen_at`, and
the source is added to `metadata_json._sources`. Nothing else on that row
changes: apply state, scores, enrichment and `module4_*` stay as they were.

## Rule filters (`policy/job_filters.py`)

The same rules run in two places:

- **At collection** (jobfound): a job that fails is never stored. The command
  output counts drops per reason.
- **After enrich, before triage** (`jobo prefilter`, every source): a failing
  row gets `prefilter_reason=<reason>`, `should_apply=0` and a
  `prefilter: <reason>` note in `module3_fit_reasoning`. Triage never selects
  it, so it costs no Claude, and it is never queued for apply. Only rows that
  have not been scored, queued, applied or closed are ever touched.
  `jobo triage --force --job-id N` is the one way to triage a prefiltered row
  anyway; its score then decides and `prefilter_reason` is cleared. A plain
  `--force` or `--queued` never re-scores prefiltered rows. `jobo funnel`
  prints the prefiltered counts per reason.

A rule runs only when its input is known. Title rules work for every row. The
pay, experience and category rules need structured values (jobfound's tags),
so LinkedIn email rows are only judged by title and an exact internship
employment type. When a value is unclear, the job goes through to triage.

| Reason | Drops a job when | Constants |
| --- | --- | --- |
| `internship` | job type is Internship, the Internship category is set, or the title matches the whole-word intern regex (`module3/exclusions.py`) | `INTERNSHIP_JOB_TYPES`, `INTERNSHIP_CATEGORIES` |
| `qa_testing` | the Quality Assurance category is set, or the title is a QA/testing title | `QA_CATEGORIES`, `QA_TITLE_RE` |
| `non_tech` | categories are known and none of them is tech. If the only categories are neutral ("Entry Level"), the title decides. | `TECH_CATEGORIES`, `NEUTRAL_CATEGORIES`, `TECH_TITLE_RE` |
| `too_senior` | the minimum years in the experience tag is at least 5 ("5++ years" counts) | `MAX_MIN_YEARS` |
| `low_pay` | the salary is in LPA and the top of its range is below 12 ("10-12 LPA" is kept) | `MIN_TOP_LPA` |

There is no location rule on purpose. Non-India jobs go to triage, and its
profile rules decide. Pay in other currencies and "Competitive" are left to
triage's pay rule. `THIN_DESCRIPTION_CHARS` (300) is the description length
below which jobfound fetches the job's own page for a fuller description.

The intern exclusion in `module3/exclusions.py` stays in triage as a backstop.

## jobfound.org

- Reads only what robots.txt allows: the Atom feed (`/feeds/jobs`, always the
  newest 500 jobs), `sitemap.xml` and `/job/<slug>` pages. It never calls
  `/api/` and never drives a browser. Requests go one at a time, with a delay
  between page fetches, a real User-Agent, retries with backoff, and respect
  for `Retry-After` and 429.
- The feed is not well-formed XML (an unescaped `&` in some apply URLs), so it
  is parsed leniently: bare `&` outside CDATA is escaped first (lxml's recover
  mode alone keeps the entry but silently drops `&source=...` from the URL),
  then lxml in recover mode, or one entry at a time. One bad entry never loses
  the batch, and skipped entries are counted.
- Each run reads the feed, then checks the sitemap for jobs posted since the
  last successful run (a cursor in the `source_state` table) that the feed did
  not include. It fetches only those pages (oldest first, at most
  `MAX_GAP_PAGES` per run; when that cap cuts the gap the cursor stops at the
  last page fetched). With no cursor yet, `--since-hours` (default 24) bounds
  the gap. The feed holds about 20 hours of jobs on a normal day, so the
  sitemap check is what guarantees nothing is missed after a long gap.
- `job_url` is the normalized apply URL. Non-LinkedIn apply links (Greenhouse,
  Ashby, Workday, iCIMS, ...) are queued for apply like any other job and go
  through the same gates (employer blocklist, circuit breaker, not-retryable
  walls, paused sites, so Rippling links wait). What differs
  (`module4/routing.py`): no LinkedIn session check and no closed-listing
  preflight (both are LinkedIn-only), and in a parallel run the job goes
  straight to a form worker instead of being routed by the LinkedIn worker (a
  direct Google Form stays on the signed-in browser). On hosts one ATS shares
  across employers (`boards.greenhouse.io/<employer>`, `jobs.ashbyhq.com`,
  `jobs.lever.co`, ...) the circuit breaker counts failures per employer, not
  per host. `jobo apply --plan` shows this per job without opening anything.
- Enrichment comes from jobfound's own data (title, company, location, salary,
  experience, categories, description), with `module2_model='jobfound'`. A
  description under 300 characters is replaced with the description from the
  job's jobfound page (the page's HTML job payload, else its JSON-LD). jobo never fetches the employer's site or
  LinkedIn to enrich these rows. The apply stage still opens the real apply
  link.

## Running it

```
env -u PYTHONPATH uv run jobo collect-jobfound --dry-run   # fetch + filter, write nothing
env -u PYTHONPATH uv run jobo collect-jobfound             # store kept jobs
env -u PYTHONPATH uv run jobo prefilter --dry-run          # what the post-enrich filter would drop
```

`JOBO_DB_PATH=/path/to/copy.db` points every command at another database
file, for example a copy for a dry run.

### Pipeline and schedule

`scripts/daily_pipeline.sh` runs the jobfound stage right after the email
collector (`jobfound` in the verdict line). A jobfound failure is recorded but
does not stop the run. The Gmail-auth abort still applies only to the email
collector. `jobo prefilter` runs between enrich and triage.

`scripts/collect_sources.sh` is a collector-only run (no enrich, triage or
apply, and no Claude). Between the 10:00 and 22:00 pipeline runs it keeps the
cursor fresh, so fewer pages need fetching. It has its own lock
(`/tmp/jobo_collect_sources.lock`), never takes the pipeline's lock, and is
safe to run while the pipeline holds the database. The database waits up to
30 s for a lock, and the insert path commits row by row. It writes to
`data/logs/collect_sources.log` and `data/logs/collect_verdicts.jsonl`.

Crontab line (not installed; add it with `crontab -e`, with `<repo>` the
absolute path of the checkout, as in the README's pipeline lines):

```
15 1,4,7,13,16,19 * * * /bin/bash <repo>/scripts/collect_sources.sh >> <repo>/data/logs/cron.log 2>&1
```

The host's crontab runs in IST. That is every 3 hours between the pipeline
runs (01:15, 04:15, 07:15, 13:15, 16:15, 19:15), at :15 so it stays clear of
the 10:00 and 22:00 starts. Together with the pipeline's own jobfound stage,
jobs are collected every 3 hours or less.

### Switch

`JOBO_JOBFOUND_ENABLED=0` turns jobfound off in both scripts. It is on by
default. Set it in the crontab line or the environment, for example
`JOBO_JOBFOUND_ENABLED=0 /bin/bash .../daily_pipeline.sh`.

## Adding a source

1. Write `sources/<name>.py`. It fetches, parses and yields `JobCandidate`s
   with `source='<name>'`, normalizing URLs through `sources.urls`. If it has
   structured facts, put them in `enrichment['_facts']` and drop failing jobs
   with `policy.job_filters` at collection time.
2. Call `sources.store.insert_candidates(session, candidates)`. Dedup,
   `_sources` and enrichment columns are handled there.
3. If the source has full job text, make the candidates pre-enriched
   (`enrichment` + `enrichment_model='<name>'`) and exclude that model in
   `module2/query.py`, so module 2 never scrapes them. Otherwise yield bare
   links and module 2 enriches them like the email jobs.
4. Add a `jobo collect-<name>` command in `main.py`, a stage in
   `scripts/daily_pipeline.sh` (with its own verdict and a
   `JOBO_<NAME>_ENABLED` switch), and a line in `scripts/collect_sources.sh`
   if it should run between pipeline runs.
5. Add offline regression tests with fictional fixtures, registered in
   `scripts/run_isolated_regressions.py`.
