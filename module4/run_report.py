"""`jobo run-report`: a short, deterministic triage report for one pipeline run (GHO-44).

After every run the post-run review (ghost) needs to know which apply failures
happened, whether they are new, and whether someone is already on them, without
reading a 5k-line log. No LLM is involved: everything here is regexes and SQL.

Which jobs a run touched comes from the run log, not from timestamps: jobs has
no per-attempt timestamp (module4_submitted_at/closed_at cover only two of the
outcomes), while every apply-stage line the runner writes carries the job id
("job_id=N: ...", "[N] Applying (attempt ...)", "skipping job N"). A job counts
as *attempted* in this run only if it has an "[N] Applying" line; a stuck job
that was merely re-dropped ("Not retrying") is reported as carried over. The
outcome itself is read from jobs.db as it is now.

Failures are clustered by (signature, ATS host):
  - host: the apply URL from the run's "[N] Routed:" line (LinkedIn job_url if
    the job was never routed), folded to its ATS family by
    module4.apply_queue.family_for_url (acme.wd5.myworkdayjobs.com -> workday).
  - signature: module4_last_status normalised (lowercase; URLs, hosts, emails,
    company name, quotes and anything with a digit stripped) and then matched
    against SIGNATURE_RULES below. The key is the rule's, never the free text,
    so the same failure gets the same signature in every run. An unmatched
    reason falls back to "other:<status prefix>".

The failure ledger (db.models.FailureLedgerModel) is the one thing this writes:
per-run rows in failure_ledger_runs are replaced, not added to, so reporting
the same log twice does not double-count.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from db.models import FailureLedgerModel, FailureLedgerRunModel, JobModel
from module4.apply_queue import company_key, family_for_url
from module4.funnel import compute_funnel, run_window_from_log

BUCKETS = ("temporary", "fixable", "needs_g")
LEDGER_STATUSES = ("open", "fixing", "fixed", "wont_fix", "needs_g")
TEXT_CLUSTER_CAP = 15

# (signature, suggested bucket, pattern on the normalised reason). First match
# wins, so narrow rules sit above broad ones: a specific network error above
# everything, the walls only G can clear above the captcha/widget rules, and
# the generic "timed out" last (an upload that ended in a timeout is an upload
# failure). Ghost makes the final bucket call; this is only the suggestion.
SIGNATURE_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (key, bucket, re.compile(pattern))
    for key, bucket, pattern in (
        ("net:dns", "temporary", r"name resolution|name_not_resolved|getaddrinfo|\bdns\b"),
        ("net:http2", "temporary", r"err_http2"),
        ("net:connection", "temporary",
         r"err_connection|err_internet|err_network|connection (?:reset|refused|closed|aborted)"
         r"|network (?:error|failure)|econnreset|socket hang up"),
        ("browser:crash", "temporary",
         r"browser (?:session )?(?:crash|died|closed unexpectedly|disconnected)|(?:page|session|tab) crashed|target closed"
         r"|aw snap|cdp (?:connection|session) (?:lost|closed)"),
        ("site:rate_limited", "temporary", r"too many (?:attempts|requests)|rate[- ]?limit"),
        # Parked behind an hCaptcha that every visitor meets (module4/captcha_walls.py):
        # nothing left to fix in code; going further is G's call (a solver service).
        ("captcha:wall_parked", "needs_g", r"\bcaptcha_wall\b"),
        ("personal:id_or_dob", "needs_g",
         r"\bpan\b|aadhaa?r|date of birth|\bdob\b|national (?:id|identifier)|passport number"
         r"|social security|\bmarks\b|\bcgpa\b|\bgpa\b|percentage"),
        ("personal:photo", "needs_g", r"\bphoto|headshot|photograph"),
        ("personal:disclosures", "needs_g", r"conflict[- ]of[- ]interest|disclosures"),
        ("signin:linkedin", "needs_g",
         r"linkedin (?:login|log in|sign[- ]?in|requires (?:a )?sign|job requires sign)|linkedin credentials"),
        ("signin:oauth_only", "needs_g",
         r"oauth|google (?:account|form)|apple (?:id|account)|microsoft account|sign[- ]?in with google"
         r"|multi[- ]factor|\bmfa\b|trusted[- ]device|push approval"),
        ("account:create_noop", "fixable",
         r"(?:create account|creat\w* (?:the |a )?(?:\w+ )?account)[^.]{0,80}"
         r"(?:no (?:page )?change|changes nothing|does nothing|no response|no navigation|nothing happen|silent|no-?op|no re|loops?)"
         r"|account creation silently|stuck at create account|fails? to create[^.]{0,30}account"),
        ("account:exists_password", "needs_g",
         r"account (?:already )?exists|already (?:has|have) an account|(?<!no )existing account|pre-existing account"
         r"|already registered|user already exists|account [^.]{0,40}exists"),
        # The agent's own budget ran out: the terminal reason, whatever form
        # step it was on (those reasons also mention uploads and dropdowns).
        ("agent:step_budget", "fixable",
         r"ran out of|step budget|tool[- ]?(?:call )?(?:iteration )?(?:limit|cap|budget)|iteration (?:limit|cap)|iterations"),
        ("otp:not_delivered", "fixable",
         r"(?:otp|verification code|one-time|code)[^.]{0,80}(?:never|not) (?:deliver|arriv|receiv)|undeliverable"),
        ("signin:otp_gated", "fixable", r"(?:sign[- ]?in|log ?in|registration)[^.]{0,60}\botp\b|otp[- ]gated"),
        ("signin:required", "needs_g",
         r"requires? (?:a )?(?:sign[- ]?in|log ?in|an account)|account sign[- ]?in|sign[- ]?in required|login wall"
         r"|sign[- ]?in to apply|\bb2c\b"),
        ("captcha:turnstile", "fixable", r"turnstile"),
        ("captcha:hcaptcha", "fixable", r"hcaptcha"),
        ("captcha:recaptcha", "fixable", r"re-?captcha"),
        ("captcha:slider", "fixable", r"datadome|slider|geetest"),
        ("bot_wall", "fixable", r"cloudflare|you have been blocked|bot[- ]protection|access denied|forbidden"),
        ("captcha:other", "fixable", r"captcha"),
        ("no_web_form", "fixable", r"mailto|no web (?:application )?form|apply (?:via|by) email|email only"),
        ("site:broken", "temporary",
         r"js exceptions?|javascript error|infinite (?:loading|spinner)|fails to load|unresponsive|blank page"),
        ("widget:value_not_kept", "fixable",
         r"will not (?:commit|retain|register|accept|keep)|won t (?:commit|retain|select)"
         r"|not retain|invalid input|dropdown|combobox|autocomplete|radio"),
        # A required answer the site's own list or catalogue does not hold (the
        # candidate's school on Ashby's school search). Above uploads, because
        # these reasons go on to say the resume was uploaded fine.
        ("field:no_true_option", "needs_g",
         r"(?:school|college|university|institution|degree|discipline|major|field of study)\b.{0,80}?"
         r"(?:no (?:\w+ ){0,2}(?:entry|entries|match|matches|option|options|result|results)\b"
         r"|not (?:in|listed|offered|found)\b|does not (?:exist|list|contain|offer)"
         r"|(?:has|have|offers?) no\b|no true\b)"),
        ("upload:failed", "fixable", r"upload|dropzone|file input|file[- ]picker"),
        ("timeout", "temporary", r"timed out|timeout"),
        ("agent:no_verdict", "fixable", r"no verdict"),
    )
)
# Identity providers and ATS vendors: a reason naming them is about the wall,
# not the employer, so they survive company stripping even when the employer's
# name contains one ("requires a google account" must keep "google").
_KEEP_WORDS = frozenset({
    "google", "apple", "microsoft", "linkedin", "facebook", "github", "oracle",
    "workday", "greenhouse", "lever", "ashby", "avature", "successfactors",
})
_RULE_BUCKET = {key: bucket for key, bucket, _ in SIGNATURE_RULES}

_URL = re.compile(r"(?:https?|mailto|ftp):\S*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
_QUOTES = re.compile(r"[\"'`‘’“”]")
# Ids, counts, years, req numbers: any token holding a digit. net::ERR_* codes
# are stable identifiers, so they survive (ERR_HTTP2_PROTOCOL_ERROR).
_DIGIT_TOKEN = re.compile(r"\b(?!err_)[a-z_]*\d[\w-]*")
_NON_WORD = re.compile(r"[^a-z0-9_:]+")


def normalise_reason(status: str | None, company: str | None = None) -> str:
    """module4_last_status with everything run- or job-specific removed."""
    s = (status or "").lower()
    s = _URL.sub(" ", s)
    s = _EMAIL.sub(" ", s)
    s = _HOST.sub(" ", s)
    s = _QUOTES.sub(" ", s)
    for phrase in _company_phrases(company):
        if phrase not in _KEEP_WORDS:
            s = re.sub(rf"\b{re.escape(phrase)}\b", " ", s)
    s = _DIGIT_TOKEN.sub(" ", s)
    s = _NON_WORD.sub(" ", s)
    return " ".join(t for t in s.split() if len(t) > 1 or t == ":")


def _company_phrases(company: str | None) -> list[str]:
    name = (company or "").lower()
    name = _QUOTES.sub(" ", name).strip()
    if not name:
        return []
    core = company_key(name).split()
    phrases = [name, " ".join(core)]
    # Single words of a multi-word name only when they are long enough not to
    # be ordinary words in a reason ("AI", "Go"); a one-word name always goes.
    phrases += [w for w in core if len(core) == 1 or len(w) >= 3]
    return sorted({p for p in phrases if p}, key=len, reverse=True)


def signature_for(status: str | None, company: str | None = None) -> str:
    """The stable short key for one failure reason."""
    text = normalise_reason(status, company)
    for key, _bucket, pattern in SIGNATURE_RULES:
        if pattern.search(text):
            return key
    prefix = (status or "").split(":", 1)[0].strip().lower() or "unknown"
    prefix = re.sub(r"[^a-z_]", "", prefix) or "unknown"
    return f"other:{prefix}"


def suggested_bucket(signature: str) -> str:
    return _RULE_BUCKET.get(signature, "fixable")


def ats_host_for_url(url: str | None) -> str:
    family = family_for_url(url)
    if family in ("linkedin.com", "lnkd.in"):
        return "linkedin"
    return family or "unknown"


def scrub_sample(status: str | None, company: str | None) -> str:
    """One raw reason for the report, minus the company name and any email."""
    s = _EMAIL.sub("<email>", status or "")
    for phrase in _company_phrases(company):
        s = re.sub(rf"(?i)\b{re.escape(phrase)}\b", "<company>", s)
    return s


# ---------------------------------------------------------------- run log


_APPLY_START = re.compile(r"=== apply starting ===")
_JOB_ID = re.compile(r"job_id=(\d+)")
_SKIPPING_JOB = re.compile(r"skipping job (\d+)")
_APPLYING = re.compile(r"\[(\d+)\] Applying\b")
_ROUTED = re.compile(r"\[(\d+)\] Routed: \w+(?: (https?://\S+))?")


@dataclass
class RunLogJobs:
    touched: set[int] = field(default_factory=set)
    attempted: set[int] = field(default_factory=set)
    routes: dict[int, str] = field(default_factory=dict)  # job id -> ats host


def jobs_from_log(log_path: Path) -> RunLogJobs:
    """Job ids from the apply section of a run log (triage also logs job_id=)."""
    out = RunLogJobs()
    in_apply = False
    with log_path.open(errors="replace") as f:
        for line in f:
            if not in_apply:
                in_apply = bool(_APPLY_START.search(line))
                continue
            for rx in (_JOB_ID, _SKIPPING_JOB):
                out.touched.update(int(m) for m in rx.findall(line))
            m = _APPLYING.search(line)
            if m:
                out.attempted.add(int(m.group(1)))
                out.touched.add(int(m.group(1)))
            m = _ROUTED.search(line)
            if m:
                jid = int(m.group(1))
                out.touched.add(jid)
                url = m.group(2)
                # No URL (easy_apply, or "unknown": Apply opened no employer
                # page) means the LinkedIn worker applied on LinkedIn itself.
                out.routes[jid] = ats_host_for_url(url) if url else "linkedin"
    return out


def find_verdict(run_log: Path, verdicts_path: Path | None = None) -> dict | None:
    """The pipeline_verdicts.jsonl line for this log (the last one, if several)."""
    path = verdicts_path or run_log.parent / "pipeline_verdicts.jsonl"
    if not path.exists():
        return None
    want_abs = str(run_log.resolve())
    found = None
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            log = str(rec.get("log") or "")
            if log == want_abs or (log and Path(log).name == run_log.name):
                found = rec
    return found


# ---------------------------------------------------------------- report


@dataclass
class Cluster:
    signature: str
    ats_host: str
    job_ids: list[int]
    count: int
    sample_reason: str
    suggested_bucket: str
    new_host: bool = False
    repeat: bool = False
    repeat_runs: int = 0
    ledger: dict | None = None


@dataclass
class RunReport:
    run_log: str
    run_ts: str
    overall: str | None
    stages: list[dict]
    budget: dict | None
    funnel: dict
    job_source: str
    jobs_touched: int
    jobs_attempted: int
    outcomes: dict[str, int]
    # Of should_apply jobs attempted in this run and still open, how many are
    # submitted now. The funnel above only counts jobs first seen in the run's
    # window, which is often empty for a run that applies a backlog.
    attempted_rate: dict
    clusters: list[Cluster]
    carried_over: list[int]

    def to_json(self) -> dict:
        return asdict(self)


def _run_ts(verdict: dict | None, log_path: Path) -> str:
    if verdict and verdict.get("ts"):
        return str(verdict["ts"])
    start, _end = run_window_from_log(log_path)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    session: Session,
    run_log: Path,
    *,
    verdicts_path: Path | None = None,
    update_ledger: bool = True,
) -> RunReport:
    run_log = Path(run_log)
    log_key = str(run_log.resolve())
    verdict = find_verdict(run_log, verdicts_path)
    run_ts = _run_ts(verdict, run_log)

    start, end = run_window_from_log(run_log)
    funnel = compute_funnel(session, since=start, until=end + timedelta(seconds=1))

    seen = jobs_from_log(run_log)
    rows = (
        session.query(JobModel).filter(JobModel.id.in_(sorted(seen.touched))).all()
        if seen.touched else []
    )
    outcomes: dict[str, int] = {}
    tried = [r for r in rows if r.id in seen.attempted and r.should_apply]
    tried_open = [r for r in tried if r.module4_outcome != "closed"]
    tried_submitted = sum(1 for r in tried_open if r.module4_outcome == "submitted")
    groups: dict[tuple[str, str], list[JobModel]] = {}
    carried: list[int] = []
    for row in sorted(rows, key=lambda r: r.id):
        outcome = row.module4_outcome or "not_attempted"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome != "stuck":
            continue
        if row.id not in seen.attempted:
            carried.append(row.id)
            continue
        host = seen.routes.get(row.id) or ats_host_for_url(row.job_url)
        sig = signature_for(row.module4_last_status, row.company_name)
        groups.setdefault((sig, host), []).append(row)

    clusters = [
        Cluster(
            signature=sig,
            ats_host=host,
            job_ids=[r.id for r in members],
            count=len(members),
            sample_reason=scrub_sample(members[0].module4_last_status, members[0].company_name)[:300],
            suggested_bucket=suggested_bucket(sig),
        )
        for (sig, host), members in groups.items()
    ]
    clusters.sort(key=lambda c: (-c.count, c.signature, c.ats_host))

    if update_ledger:
        upsert_run(session, log_key, run_ts, clusters)
    _annotate_from_ledger(session, clusters, log_key, run_ts)

    return RunReport(
        run_log=log_key,
        run_ts=run_ts,
        overall=(verdict or {}).get("overall"),
        stages=list((verdict or {}).get("stages") or []),
        budget=(verdict or {}).get("budget"),
        funnel={
            "should_apply_total": funnel.should_apply_total,
            "closed": funnel.closed,
            "still_open": funnel.still_open,
            "submitted": funnel.submitted,
            "stuck": funnel.stuck,
            "skipped_by_policy": funnel.skipped_by_policy,
            "not_attempted": funnel.not_attempted,
            "apply_rate": funnel.apply_rate,
        },
        job_source="run log (apply-section job_id=N / [N] Applying / skipping job N); "
        "outcomes from jobs.db now",
        jobs_touched=len(rows),
        jobs_attempted=len(seen.attempted & {r.id for r in rows}),
        outcomes=dict(sorted(outcomes.items())),
        attempted_rate={
            "attempted": len(tried),
            "still_open": len(tried_open),
            "submitted": tried_submitted,
            "rate": (tried_submitted / len(tried_open)) if tried_open else None,
        },
        clusters=clusters,
        carried_over=carried,
    )


# ---------------------------------------------------------------- ledger


def upsert_run(session: Session, run_log: str, run_ts: str, clusters: list[Cluster]) -> None:
    """Replace this run's ledger rows, then recount every key it touched."""
    old = session.query(FailureLedgerRunModel).filter_by(run_log=run_log).all()
    keys = {(r.signature, r.ats_host) for r in old}
    for r in old:
        session.delete(r)
    session.flush()
    for c in clusters:
        session.add(FailureLedgerRunModel(
            signature=c.signature, ats_host=c.ats_host, run_log=run_log,
            run_ts=run_ts, job_ids_json=json.dumps(sorted(c.job_ids)),
        ))
        keys.add((c.signature, c.ats_host))
    session.flush()
    for sig, host in keys:
        _recount(session, sig, host)
    session.commit()


def _recount(session: Session, signature: str, host: str) -> None:
    runs = (
        session.query(FailureLedgerRunModel)
        .filter_by(signature=signature, ats_host=host)
        .all()
    )
    entry = session.get(FailureLedgerModel, (signature, host))
    if entry is None:
        if not runs:
            return
        entry = FailureLedgerModel(signature=signature, ats_host=host, status="open")
        session.add(entry)
    job_ids: set[int] = set()
    for r in runs:
        job_ids.update(json.loads(r.job_ids_json or "[]"))
    stamps = sorted(r.run_ts for r in runs)
    entry.first_seen = stamps[0] if stamps else None
    entry.last_seen = stamps[-1] if stamps else None
    entry.seen_runs = len(runs)
    entry.job_count = len(job_ids)
    entry.updated_at = datetime.now()


def _annotate_from_ledger(session: Session, clusters: list[Cluster], run_log: str, run_ts: str) -> None:
    earlier = (
        session.query(FailureLedgerRunModel)
        .filter(FailureLedgerRunModel.run_log != run_log)
        .filter(FailureLedgerRunModel.run_ts < run_ts)
        .all()
    )
    hosts_before = {r.ats_host for r in earlier}
    runs_before: dict[tuple[str, str], set[str]] = {}
    for r in earlier:
        runs_before.setdefault((r.signature, r.ats_host), set()).add(r.run_log)
    for c in clusters:
        c.new_host = c.ats_host not in hosts_before
        c.repeat_runs = len(runs_before.get((c.signature, c.ats_host), ()))
        c.repeat = c.repeat_runs > 0
        entry = session.get(FailureLedgerModel, (c.signature, c.ats_host))
        if entry is not None:
            c.ledger = ledger_dict(entry)


def ledger_dict(entry: FailureLedgerModel) -> dict:
    return {
        "signature": entry.signature,
        "ats_host": entry.ats_host,
        "first_seen": entry.first_seen,
        "last_seen": entry.last_seen,
        "seen_runs": entry.seen_runs,
        "job_count": entry.job_count,
        "bucket": entry.bucket,
        "issue": entry.issue,
        "fix_commit": entry.fix_commit,
        "status": entry.status,
    }


def ledger_set(
    session: Session,
    signature: str,
    *,
    host: str | None = None,
    bucket: str | None = None,
    issue: str | None = None,
    fix_commit: str | None = None,
    status: str | None = None,
) -> list[FailureLedgerModel]:
    """Record triage on one (signature, host), or on every host of a signature."""
    if bucket is not None and bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {', '.join(BUCKETS)}")
    if status is not None and status not in LEDGER_STATUSES:
        raise ValueError(f"status must be one of {', '.join(LEDGER_STATUSES)}")
    q = session.query(FailureLedgerModel).filter_by(signature=signature)
    if host is not None:
        q = q.filter_by(ats_host=host)
    entries = q.all()
    if not entries:
        if host is None:
            raise LookupError(f"no ledger entry for signature {signature!r}; pass --host to create one")
        entry = FailureLedgerModel(signature=signature, ats_host=host, status="open",
                                   seen_runs=0, job_count=0)
        session.add(entry)
        entries = [entry]
    for e in entries:
        if bucket is not None:
            e.bucket = bucket
        if issue is not None:
            e.issue = issue
        if fix_commit is not None:
            e.fix_commit = fix_commit
        if status is not None:
            e.status = status
        e.updated_at = datetime.now()
    session.commit()
    return entries


def ledger_list(session: Session, status: str | None = None) -> list[FailureLedgerModel]:
    q = session.query(FailureLedgerModel)
    if status is not None:
        q = q.filter_by(status=status)
    return q.order_by(FailureLedgerModel.last_seen.desc(), FailureLedgerModel.signature).all()


def render_ledger(entries: list[FailureLedgerModel]) -> str:
    if not entries:
        return "failure ledger: no entries"
    lines = [f"{'signature':28s} {'host':20s} {'status':9s} {'bucket':9s} {'issue':9s} runs jobs  last_seen             fix_commit"]
    for e in entries:
        lines.append(
            f"{e.signature:28s} {e.ats_host:20s} {e.status or '-':9s} {e.bucket or '-':9s} "
            f"{e.issue or '-':9s} {e.seen_runs:4d} {e.job_count:4d}  {e.last_seen or '-':21s} {e.fix_commit or '-'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- text


def _pct(value) -> str:
    return "?" if value is None else f"{float(value):.0%}"


def render_text(report: RunReport, cap: int = TEXT_CLUSTER_CAP) -> str:
    lines = [f"jobo run report: {Path(report.run_log).name}"]
    lines.append(f"  run ts {report.run_ts}   overall {report.overall or 'NO VERDICT LINE'}")
    if report.stages:
        lines.append("  stages: " + ", ".join(f"{s.get('stage')} {s.get('verdict')}" for s in report.stages))
    b = report.budget
    if b:
        lines.append(
            f"  budget gate ({b.get('gate')}): {b.get('decision')}, weekly {_pct(b.get('utilization'))}"
            f" (threshold {_pct(b.get('threshold'))}), session {_pct(b.get('session_utilization'))}"
            + (f", error: {b['error']}" if b.get("error") else "")
        )
    f = report.funnel
    rate = "undefined" if f["apply_rate"] is None else f"{f['apply_rate']:.1%}"
    lines.append(
        f"  funnel (jobs first seen this run): apply rate {rate} ({f['submitted']}/{f['still_open']} still open;"
        f" stuck {f['stuck']}, not attempted {f['not_attempted']}, closed {f['closed']})"
    )
    outcomes = ", ".join(f"{k} {v}" for k, v in report.outcomes.items()) or "none"
    lines.append(
        f"  apply stage touched {report.jobs_touched} jobs, attempted {report.jobs_attempted}: {outcomes}"
    )
    a = report.attempted_rate
    a_rate = "undefined" if a["rate"] is None else f"{a['rate']:.1%}"
    lines.append(
        f"  attempted this run (should_apply): submitted {a_rate} ({a['submitted']}/{a['still_open']} still open,"
        f" {a['attempted'] - a['still_open']} closed)"
    )
    lines.append(f"  jobs from: {report.job_source}")
    failed = sum(c.count for c in report.clusters)
    lines.append("")
    lines.append(f"Failure clusters: {len(report.clusters)} ({failed} stuck jobs attempted this run)")
    for i, c in enumerate(report.clusters[:cap], 1):
        flags = [f"suggest {c.suggested_bucket}"]
        if c.new_host:
            flags.append("NEW HOST")
        if c.repeat:
            flags.append(f"repeat x{c.repeat_runs} earlier run{'s' if c.repeat_runs != 1 else ''}")
        led = c.ledger or {}
        ledger = (
            f"ledger {led.get('status')}"
            + (f" {led['issue']}" if led.get("issue") else "")
            + (f" bucket={led['bucket']}" if led.get("bucket") else "")
            + (f" fix={led['fix_commit']}" if led.get("fix_commit") else "")
        ) if led else "ledger -"
        lines.append(f"{i:2d}. {c.signature} @ {c.ats_host}  x{c.count}  [{'; '.join(flags)}]  {ledger}")
        lines.append(f"    jobs: {', '.join(str(j) for j in c.job_ids)}")
        lines.append(f"    sample: {c.sample_reason[:200]}")
    if len(report.clusters) > cap:
        lines.append(f"    ... {len(report.clusters) - cap} more clusters (use --json for all)")
    if report.carried_over:
        lines.append("")
        lines.append(
            f"Carried over (stuck from an earlier run, not re-attempted here): {len(report.carried_over)} jobs: "
            + ", ".join(str(j) for j in report.carried_over)
        )
    return "\n".join(lines)
