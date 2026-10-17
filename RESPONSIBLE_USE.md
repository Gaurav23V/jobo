# Responsible use

jobo does not draft applications for you to review. It fills in real
application forms and **submits** them to real employers under your name, with
no one checking each one before it goes out. Treat every run as if you had
clicked "Submit" on each of those jobs yourself.

- **Only apply to jobs you would really take.** Set up `personal/profile.md`,
  the fit threshold and the exclusions so that a job jobo applies to is one you
  would accept if offered. Applying to jobs you don't want wastes recruiters'
  time and your own.
- **Keep everything truthful.** The agent copies your facts from `personal/`
  into forms, and triage rewrites the Skills and Projects sections of your
  resume. Keep `personal/` accurate, and read some of the tailored resumes and
  cover letters it makes. If the model overstates something, correct your
  profile and resume so it can't happen again.
- **Apply for yourself only.** jobo is meant for one person applying for their
  own jobs. Don't run it for other people or with made-up identities.
- **Respect the sites you use.** You are responsible for following the terms of
  LinkedIn, Gmail, the job boards and every employer site that jobo uses for you.
  If a site doesn't allow automated applications, don't point jobo at it: add
  its domain to `JOBO_PAUSED_SITES`, or the employer to `JOBO_BLOCKED_EMPLOYERS`
  (`policy/employer_blocklist.py`).
- **Captchas and bot checks are not bypassed.** When a site shows a captcha or
  a bot check jobo can't legitimately get past, the job is skipped and recorded.
  Don't add code to get around them.
- **Keep the rate limits.** jobo waits after each submission, applies to each
  job at most a few times, stops on sites that keep failing, and stops when your
  Claude usage limits are reached. Don't remove these limits or run several
  instances at once to go faster.
- **Watch it.** Check the run logs in `data/logs/` and `jobo funnel` after the
  first runs, and from time to time after that. Sites change and models make
  mistakes; a wrong answer goes to a real employer.
- **Protect your data.** `personal/`, `credentials/`, `.env` and `data/` hold
  your details, your Gmail access, a job-site password and your application
  history. They are gitignored; keep it that way, and back up `data/` only to a
  private repo (`scripts/backup_data.sh`).

**You are responsible for what jobo sends.** The software comes with no warranty
(see [LICENSE](LICENSE)).
