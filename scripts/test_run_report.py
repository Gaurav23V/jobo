#!/usr/bin/env python3
"""Test module4.run_report: failure signatures, clustering, the failure ledger (GHO-44).

The reason strings in section 1 follow real module4_last_status rows in
data/jobs.db (2026-10-18) word for word in their failure wording; only the
employer names, hosts, emails and ids are swapped for fictional ones. Each
real failure is given twice with different employer/host/number details,
because the point of a signature is that both copies get the same key.

Run: bash scripts/run_regression_tests.sh test_run_report
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, FailureLedgerModel, FailureLedgerRunModel, JobModel  # noqa: E402
from module4 import run_report as rr  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:70s} {note}")


# (expected signature, bucket, [(status, company), (status, company)])
REAL_SHAPES = [
    ("captcha:turnstile", "fixable", [
        ("captcha_failed:Treyco's apply modal is stuck at \"Reading your resume...\" because its "
         "Cloudflare Turnstile widget hangs and errors (300030) in an infinite retry loop", "Treyco"),
        ("captcha_failed:Osprey's Apply modal hangs indefinitely on Cloudflare Turnstile (error 600010, "
         "\"Turnstile Widget seem to have hung\") during the resume-parse step", "Northwind Labs"),
    ]),
    ("account:exists_password", "needs_g", [
        ("stuck:Northwind's Avature portal (careers.northwind.com) has a pre-existing account for "
         "someone@example.com; password reset rejected the standard reset password", "Northwind"),
        ("stuck:Treyco SuccessFactors (career44.sapsf.com) account for other@example.org already exists "
         "from a prior attempt (blocking new registration)", "Treyco Pvt Ltd"),
    ]),
    ("account:create_noop", "fixable", [
        ("stuck:Workday account creation silently fails - Create Account and Forgot Password buttons "
         "produce no page change, no error, and no navigation", "Northwind"),
        ("stuck:Treyco's Workday application portal (treyco.wd5.myworkdayjobs.com) is stuck at Create "
         "Account/Sign In step 1 of 7 - the Create Account button does nothing", "Treyco"),
    ]),
    ("personal:id_or_dob", "needs_g", [
        ("stuck:Northwind Bank application step 2 mandates PAN (Indian tax ID) and Date of Birth, "
         "neither of which is in the candidate facts", "Northwind Bank"),
        ("stuck:Treyco's application form (careers.treyco.com) requires PAN number and Date of Birth as "
         "mandatory fields on the \"My information\" step", "Treyco"),
    ]),
    ("personal:photo", "needs_g", [
        ("stuck:Northwind Avature form requires a mandatory Candidate Photo (passport-style photograph) "
         "upload field; no photo asset exists", "Northwind"),
        ("stuck:Workday step 5 of 7 requires a mandatory \"Photo Upload\" (a current headshot of the "
         "candidate) which is not available", "Treyco"),
    ]),
    ("signin:oauth_only", "needs_g", [
        ("stuck:Apply flow routes to a Google Form requiring Google Account sign-in only, no "
         "email/password option available", "Northwind"),
        # The employer's own name is an identity provider: company stripping
        # must not erase "google" from "google account".
        ("stuck:Google Careers apply flow forces sign-in/account creation via accounts.google.com Google "
         "Account, with no email/password ATS alternative", "Google"),
    ]),
    ("agent:step_budget", "fixable", [
        ("stuck:ran out of step budget (100 model steps) mid-form, before submitting", None),
        ("stuck:Ran out of tool-call iterations after uploading resume and filling personal info on "
         "Treyco's Workday-style form", "Treyco"),
    ]),
    ("upload:failed", "fixable", [
        ("stuck:Eightfold.ai Resume dropzone upload failed on all 4 NEED_UPLOAD attempts", "Northwind"),
        ("stuck:Resume upload on Treyco's Phenom/Taleo apply form fails all 3 NEED_UPLOAD attempts "
         "(file inputs present in DOM but undetected by the uploader)", "Treyco"),
    ]),
    ("widget:value_not_kept", "fixable", [
        ("stuck:Workday \"How Did You Hear About Us?\" required combobox (My Information step) will not "
         "retain a selected value through blur/Save", "Northwind"),
        ("stuck:Shine.com's \"Current Salary\" react-dropdown on this job's apply form will not commit "
         "any selection (tried NEED_FIELD x3)", "Treyco"),
    ]),
    ("no_web_form", "fixable", [
        ("stuck:Job posting has no web application form, only a mailto: link requiring manual email "
         "composition with resume attachment", "Northwind"),
        ("stuck:Treyco.ai posting has no web application form, only \"Apply via email\" "
         "(mailto:careers@treyco.ai)", "Treyco"),
    ]),
    ("net:http2", "temporary", [
        ("stuck:Company site careers.northwind.co.in returns ERR_HTTP2_PROTOCOL_ERROR in the browser on "
         "every attempt", "Northwind"),
        ("stuck:careers.treyco.com: net::ERR_HTTP2_PROTOCOL_ERROR on load (3 tries)", "Treyco"),
    ]),
    ("net:dns", "temporary", [
        ("stuck:navigation failed: getaddrinfo ENOTFOUND careers.northwind.com", "Northwind"),
        ("stuck:URLError: <urlopen error [Errno -3] Temporary failure in name resolution>", "Treyco"),
    ]),
    ("timeout", "temporary", [
        ("stuck:agent timed out after 480s (no verdict printed)", None),
        ("stuck:agent timed out after 900s (no verdict printed)", "Treyco"),
    ]),
    ("captcha:hcaptcha", "fixable", [
        ("captcha_failed:Lever form fully completed and resume attached, but submit is gated by an "
         "hCaptcha image-grid challenge", "Northwind"),
        ("captcha_failed:hCaptcha \"click the two ends of the line\" visual challenge (rendered inside a "
         "cross-origin enclave iframe on jobs.lever.co) blocked submit", "Treyco"),
    ]),
]


def section_signatures() -> None:
    print("1. real module4_last_status shapes map to stable signature keys")
    for expected, bucket, pairs in REAL_SHAPES:
        got = [rr.signature_for(s, c) for s, c in pairs]
        check(f"{expected}: both copies", got == [expected, expected], f"got {got}")
        check(f"{expected}: suggested bucket {bucket}", rr.suggested_bucket(expected) == bucket)

    norm = rr.normalise_reason(
        "stuck:Northwind's portal (careers.northwind.com, https://x.northwind.com/apply?id=42) "
        "for a@b.com said \"Error 300030\" at step 4", "Northwind")
    check("normalise strips company, host, url, email, quotes, numbers",
          not any(t in norm for t in ("northwind", "http", "@", "\"", "42", "300030")), repr(norm))
    check("normalise keeps the failure words", "portal" in norm and "error" in norm, repr(norm))
    check("unmatched reason falls back to other:<prefix>",
          rr.signature_for("failed:something nobody has seen yet", "Treyco") == "other:failed")
    check("same signature with and without company", rr.signature_for(
        "stuck:Treyco's Cloudflare Turnstile never issued a token", "Treyco")
        == rr.signature_for("stuck:Cloudflare Turnstile never issued a token", None))
    check("ATS subdomains fold to the vendor",
          [rr.ats_host_for_url(u) for u in (
              "https://northwind.wd5.myworkdayjobs.com/x", "https://boards.greenhouse.io/treyco/1",
              "https://jobs.lever.co/treyco/abc", "https://jobs.ashbyhq.com/n/1",
              "https://www.linkedin.com/jobs/view/1/")]
          == ["workday", "greenhouse", "lever", "ashby", "linkedin"])
    sample = rr.scrub_sample("stuck:Treyco's form for me@example.com broke", "Treyco")
    check("sample reason drops company and email",
          "Treyco" not in sample and "example.com" not in sample, sample)


# ------------------------------------------------------------ run fixtures


def make_db(tmp: Path):
    engine = create_engine(f"sqlite:///{tmp / 'jobs.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_job(session, jid, *, outcome, status=None, company="Northwind", should_apply=True):
    session.add(JobModel(
        id=jid, company_name=company, job_title="Engineer", applied=outcome == "submitted",
        job_url=f"https://www.linkedin.com/jobs/view/{jid}/", should_apply=should_apply,
        first_seen_at=datetime(2026, 1, 1), last_seen_at=datetime(2026, 1, 1),
        module4_last_status=status, module4_outcome=outcome,
        module4_attempts=1 if status else 0,
    ))


def write_log(path: Path, day: str, lines: list[str], hour: str = "10") -> Path:
    body = [
        f"[{day} {hour}:00:01] === collector starting ===",
        "2026 - module3.runner - INFO - job_id=999: triage line that is not an apply job",
        f"[{day} {hour}:10:00] === apply starting ===",
        *lines,
        f"[{day} {hour}:40:00] === apply OK ===",
        f"[{day} {hour}:40:00] === run complete, failed=0 ===",
    ]
    path.write_text("\n".join(body) + "\n")
    return path


def applying(jid: int) -> str:
    return f"x - [f1] module4.runner - INFO - [{jid}] Applying (attempt 1/3, first try): Engineer"


def routed(jid: int, url: str | None) -> str:
    tail = f"external {url} (Apply opened)" if url else "easy_apply  (Easy Apply button)"
    return f"x - [li] module4.runner - INFO - [{jid}] Routed: {tail}"


TURNSTILE = "captcha_failed:{c}'s apply modal hangs on a Cloudflare Turnstile widget (error {n})"
WD_NOOP = ("stuck:Workday account creation silently fails - Create Account produces no page change "
           "(req {n})")


def section_clustering(tmp: Path) -> None:
    print("\n2. clustering by (signature, ATS host)")
    session = make_db(tmp)
    add_job(session, 1, outcome="stuck", status=WD_NOOP.format(n=11), company="Northwind")
    add_job(session, 2, outcome="stuck", status=WD_NOOP.format(n=22), company="Treyco")
    add_job(session, 3, outcome="stuck", status=TURNSTILE.format(c="Osprey", n=300030), company="Osprey")
    add_job(session, 4, outcome="stuck", status=TURNSTILE.format(c="Treyco", n=600010), company="Treyco")
    add_job(session, 5, outcome="submitted", status="submitted")
    add_job(session, 6, outcome="closed", status="closed:no longer accepting")
    add_job(session, 7, outcome="stuck", status="stuck:hCaptcha image grid", company="Kestrel")
    session.commit()
    log = write_log(tmp / "run_20260101_1000.log", "2026-01-01", [
        routed(1, "https://northwind.wd5.myworkdayjobs.com/en-US/External/job/1"),
        applying(1),
        routed(2, "https://treyco.wd3.myworkdayjobs.com/Careers/job/2"),
        applying(2),
        routed(3, "https://jobs.talentboard.example/job/abc"), applying(3),
        routed(4, None), applying(4),
        routed(5, None), applying(5),
        "x - [li] module4.runner - INFO - - [li] job_id=6: closed via preflight (gone)",
        # Re-dropped without a new attempt: carried over, not this run's failure.
        "x - [li] module4.runner - INFO - - [li] job_id=7: DROPPED, not retryable (captcha)",
    ])
    verdicts = tmp / "pipeline_verdicts.jsonl"
    verdicts.write_text(json.dumps({
        "ts": "2026-01-01T04:40:00Z", "overall": "OK",
        "stages": [{"stage": "apply", "verdict": "OK"}],
        "budget": {"gate": "triage", "decision": "PROCEED", "utilization": 0.2, "threshold": 0.5},
        "log": str(log.resolve()),
    }) + "\n")

    report = rr.build_report(session, log)
    by_key = {(c.signature, c.ats_host): c for c in report.clusters}
    check("two workday tenants, same failure -> one cluster",
          by_key.get(("account:create_noop", "workday")) is not None
          and by_key[("account:create_noop", "workday")].job_ids == [1, 2],
          str(sorted(by_key)))
    check("same signature on two hosts -> two clusters",
          ("captcha:turnstile", "talentboard.example") in by_key and ("captcha:turnstile", "linkedin") in by_key)
    check("largest cluster first", report.clusters[0].count == 2)
    check("carried-over stuck job kept out of clusters", report.carried_over == [7]
          and all(7 not in c.job_ids for c in report.clusters), str(report.carried_over))
    check("triage job_id= line before apply is ignored", 999 not in [j for c in report.clusters for j in c.job_ids])
    check("verdict line matched by log path", report.overall == "OK" and report.run_ts == "2026-01-01T04:40:00Z")
    check("outcome counts over touched jobs",
          report.outcomes == {"closed": 1, "stuck": 5, "submitted": 1}, str(report.outcomes))
    check("attempted rate: 1 of 5 still-open attempted jobs submitted",
          report.attempted_rate["submitted"] == 1 and report.attempted_rate["still_open"] == 5,
          str(report.attempted_rate))
    check("first run: every host is new, nothing repeats",
          all(c.new_host and not c.repeat for c in report.clusters))
    check("sample reason is scrubbed of the company",
          "Osprey" not in by_key[("captcha:turnstile", "talentboard.example")].sample_reason)
    text = rr.render_text(report)
    check("text report shows the cluster and the ledger state",
          "account:create_noop @ workday  x2" in text and "ledger open" in text, text.splitlines()[0])
    as_json = json.loads(json.dumps(report.to_json(), default=str))
    check("--json carries the same clusters", len(as_json["clusters"]) == len(report.clusters))

    print("\n3. ledger idempotency and repeats")
    rr.build_report(session, log)
    rr.build_report(session, log)
    entry = session.get(FailureLedgerModel, ("account:create_noop", "workday"))
    check("same log three times: seen_runs stays 1", entry.seen_runs == 1, f"seen_runs={entry.seen_runs}")
    check("same log three times: job_count stays 2", entry.job_count == 2, f"job_count={entry.job_count}")
    check("one per-run row per cluster",
          session.query(FailureLedgerRunModel).count() == len(report.clusters))

    rr.ledger_set(session, "account:create_noop", bucket="fixable", issue="GHO-999", status="fixing")
    # A later run (later log start, no verdict line) where job 2 fails again
    # the same way and a brand-new host shows up.
    add_job(session, 8, outcome="stuck", status=WD_NOOP.format(n=33), company="Osprey")
    add_job(session, 9, outcome="stuck", status="stuck:Resume dropzone upload failed", company="Kestrel")
    session.commit()
    log2 = write_log(tmp / "run_20260101_2200.log", "2026-01-01", [
        routed(2, "https://treyco.wd3.myworkdayjobs.com/x"), applying(2),
        routed(8, "https://osprey.wd1.myworkdayjobs.com/x"), applying(8),
        routed(9, "https://kestrel.example/careers"), applying(9),
    ], hour="22")
    r2 = rr.build_report(session, log2)
    k2 = {(c.signature, c.ats_host): c for c in r2.clusters}
    wd = k2[("account:create_noop", "workday")]
    check("second run: workday cluster is a repeat of 1 earlier run", wd.repeat and wd.repeat_runs == 1)
    check("second run: workday is not a new host", not wd.new_host)
    check("second run: kestrel.example is a new host", k2[("upload:failed", "kestrel.example")].new_host)
    check("report shows the triage ghost recorded",
          wd.ledger and wd.ledger["issue"] == "GHO-999" and wd.ledger["status"] == "fixing")
    entry = session.get(FailureLedgerModel, ("account:create_noop", "workday"))
    session.refresh(entry)
    check("ledger counts across runs: 2 runs, 3 distinct jobs",
          entry.seen_runs == 2 and entry.job_count == 3, f"{entry.seen_runs}/{entry.job_count}")
    check("upsert keeps triage fields", entry.bucket == "fixable" and entry.status == "fixing")
    rr.build_report(session, log)
    session.refresh(entry)
    check("re-reporting the older log still counts 2 runs", entry.seen_runs == 2)
    old = rr.build_report(session, log)
    old_wd = {(c.signature, c.ats_host): c for c in old.clusters}[("account:create_noop", "workday")]
    check("older log: the later run does not count as an earlier repeat", not old_wd.repeat)

    print("\n4. ledger CLI helpers")
    try:
        rr.ledger_set(session, "no:such", status="open")
        check("set on unknown signature without --host refuses", False)
    except LookupError:
        check("set on unknown signature without --host refuses", True)
    rr.ledger_set(session, "no:such", host="lever", status="wont_fix")
    check("set with --host creates the row",
          session.get(FailureLedgerModel, ("no:such", "lever")).status == "wont_fix")
    try:
        rr.ledger_set(session, "no:such", host="lever", status="done")
        check("unknown status refused", False)
    except ValueError:
        check("unknown status refused", True)
    check("list --status filters", [e.signature for e in rr.ledger_list(session, "wont_fix")] == ["no:such"])
    check("list renders", "account:create_noop" in rr.render_ledger(rr.ledger_list(session)))

    print("\n5. text output caps at 15 clusters")
    many = rr.RunReport(
        run_log="x.log", run_ts="t", overall="OK", stages=[], budget=None,
        funnel={"apply_rate": None, "submitted": 0, "still_open": 0, "stuck": 0,
                "not_attempted": 0, "closed": 0},
        job_source="", jobs_touched=0, jobs_attempted=0, outcomes={},
        attempted_rate={"attempted": 0, "still_open": 0, "submitted": 0, "rate": None},
        clusters=[rr.Cluster(f"sig{i}", "h", [i], 1, "r", "fixable") for i in range(18)],
        carried_over=[],
    )
    text = rr.render_text(many)
    check("15 shown, 3 more counted", "15. sig14" in text and "16. " not in text
          and "3 more clusters" in text)


def main() -> int:
    section_signatures()
    with tempfile.TemporaryDirectory() as tmp:
        section_clustering(Path(tmp))
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nFailures:")
        for label, ok, note in results:
            if not ok:
                print(f"  {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
