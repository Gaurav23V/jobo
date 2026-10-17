"""Offline tests for the shared insert path (sources/store.py, GHO-41 part b).

Cross-source dedup (url, LinkedIn id, company+title window), seen-in
bookkeeping on duplicates, pre-enriched vs bare-link inserts, module 1 routed
through insert_candidates, and module 2 keeping ``_sources``. Every check runs
on a throwaway SQLite file with fictional employers and job ids, never
data/jobs.db.

Run: bash scripts/run_regression_tests.sh test_source_dedup
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
_TMP = Path(tempfile.mkdtemp(prefix="jobo-dedup-test-"))
os.environ["JOBO_DB_PATH"] = str(_TMP / "unused.db")  # nothing may reach data/jobs.db

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import module1.runner as m1_runner  # noqa: E402
from db.models import Base, JobModel  # noqa: E402
from module1.collector import RawEmail  # noqa: E402
from module2.enrichment_schema import EnrichmentOutput  # noqa: E402
from module2.persist import save_enrichment_result  # noqa: E402
from sources.base import JobCandidate  # noqa: E402
from sources.store import insert_candidates, normalize_company  # noqa: E402

PASS = FAIL = 0
NOW = datetime(2026, 10, 15, 9, 0, 0)


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


_n = 0


def fresh_session():
    global _n
    _n += 1
    engine = create_engine(f"sqlite:///{_TMP / f'jobs{_n}.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed(session, **kw) -> JobModel:
    first = kw.pop("first_seen_at", NOW - timedelta(days=2))
    for key, value in (("applied", False), ("referred_json", "[]"), ("source_platform", "linkedin"),
                       ("metadata_json", "{}")):
        kw.setdefault(key, value)
    row = JobModel(first_seen_at=first, last_seen_at=first, **kw)
    session.add(row)
    session.commit()
    return row


def meta(session, row_id: int):
    session.expire_all()
    return json.loads(session.get(JobModel, row_id).metadata_json)


def test_url_and_linkedin_id() -> None:
    print("url / LinkedIn id match")
    s = fresh_session()
    gh = seed(s, job_url="https://boards.example-ats.test/fabrikam/jobs/123?gh_jid=123",
              company_name="Fabrikam", job_title="Data Engineer")
    li = seed(s, job_url="https://www.linkedin.com/jobs/view/4000000201/",
              company_name="Contoso", job_title="Backend Engineer")
    r = insert_candidates(s, [
        JobCandidate(job_url="https://boards.example-ats.test/fabrikam/jobs/123?gh_jid=123&utm_source=jobfound",
                     source="jobfound"),
        JobCandidate(job_url="https://in.linkedin.com/jobs/view/backend-engineer-at-contoso-4000000201?source=jobfound.org",
                     source="jobfound"),
    ], now=NOW)
    check("both are duplicates", r.inserted == 0 and r.duplicates == 2, str(r))
    # the slug candidate normalizes to the stored canonical link, so it is a url match
    check("kinds url (ATS params + canonical LinkedIn)", r.duplicates_by == {"url": 2}, str(r.duplicates_by))
    check("seen-in seeded with own source then appended", meta(s, li.id)["_sources"] == ["linkedin", "jobfound"],
          str(meta(s, li.id)))
    check("url row seen-in", meta(s, gh.id)["_sources"] == ["linkedin", "jobfound"])
    # /comm/ and bare id written differently than the stored slug form
    seed(s, job_url="https://in.linkedin.com/jobs/view/ml-engineer-at-tailspin-4000000202",
         company_name="Tailspin", job_title="ML Engineer")
    r = insert_candidates(s, [JobCandidate(job_url="https://www.linkedin.com/comm/jobs/view/4000000202?trackingId=x",
                                           source="linkedin")], now=NOW)
    check("slug-id stored vs /comm/ candidate -> linkedin_id duplicate",
          r.duplicates_by == {"linkedin_id": 1} and r.inserted == 0, str(r))
    seed(s, job_url="https://www.linkedin.com/jobs/view/4000000203/", company_name="Contoso", job_title="SRE")
    r = insert_candidates(s, [JobCandidate(job_url="https://www.linkedin.com/jobs/view/4000000203",
                                           source="jobfound")], now=NOW)
    check("bare id without slash vs canonical -> duplicate", r.duplicates == 1 and r.inserted == 0, str(r))


def test_company_title_window() -> None:
    print("company+title fallback")
    s = fresh_session()
    old = seed(s, job_url="https://jobs.example-ats.test/northwind/1", company_name="Northwind Traders Pvt. Ltd.",
               job_title="Senior Data Engineer", first_seen_at=NOW - timedelta(days=10))
    r = insert_candidates(s, [JobCandidate(job_url="https://careers.northwind.test/apply/77", source="jobfound",
                                           company_name="NORTHWIND TRADERS", job_title="Senior  Data-Engineer")],
                          now=NOW)
    check("suffix/case/punct variant inside window -> duplicate",
          r.duplicates_by == {"company_title": 1} and r.inserted == 0, str(r))
    check("seen-in updated", meta(s, old.id)["_sources"] == ["linkedin", "jobfound"])

    seed(s, job_url="https://jobs.example-ats.test/litware/1", company_name="Litware Inc",
         job_title="Platform Engineer", first_seen_at=NOW - timedelta(days=45))
    r = insert_candidates(s, [JobCandidate(job_url="https://careers.litware.test/9", source="jobfound",
                                           company_name="Litware", job_title="Platform Engineer")], now=NOW)
    check("outside window -> inserted", r.inserted == 1 and r.duplicates == 0, str(r))

    seed(s, job_url="https://www.linkedin.com/jobs/view/4000000301/", company_name="Adatum Ltd",
         job_title="Analytics Engineer")
    r = insert_candidates(s, [JobCandidate(job_url="https://www.linkedin.com/jobs/view/4000000302/",
                                           source="jobfound", company_name="Adatum",
                                           job_title="Analytics Engineer")], now=NOW)
    check("different LinkedIn ids, same company+title -> NOT merged", r.inserted == 1 and r.duplicates == 0, str(r))
    check("normalize keeps non-legal words", normalize_company("Acme Technologies Pvt Ltd") == "acme technologies")
    check("unicode fold", normalize_company("Société Générale S.A. GmbH") == "societe generale s a")


def test_in_batch_and_dry_run() -> None:
    print("in-batch duplicates and dry run")
    s = fresh_session()
    batch = [
        JobCandidate(job_url="https://www.linkedin.com/jobs/view/4000000401/", source="linkedin"),
        JobCandidate(job_url="https://in.linkedin.com/jobs/view/x-4000000401?source=jobfound.org", source="jobfound"),
        JobCandidate(job_url="https://careers.fourthcoffee.test/1", source="jobfound",
                     company_name="Fourth Coffee LLC", job_title="Data Scientist", enrichment={"a": 1}),
        JobCandidate(job_url="https://careers.fourthcoffee.test/2", source="jobfound",
                     company_name="Fourth Coffee", job_title="Data Scientist", enrichment={"a": 2}),
    ]
    r = insert_candidates(s, batch, dry_run=True, now=NOW)
    check("dry run: counts computed", r.inserted == 2 and r.duplicates == 2, str(r))
    check("dry run: nothing written", s.query(JobModel).count() == 0)
    r = insert_candidates(s, batch, now=NOW)
    check("real run: 2 inserted, 2 in-batch duplicates",
          r.inserted == 2 and r.duplicates_by == {"url": 1, "company_title": 1}, str(r))
    check("row count 2", s.query(JobModel).count() == 2)
    first = s.get(JobModel, r.inserted_ids[0])
    check("in-batch dup recorded in seen-in", json.loads(first.metadata_json)["_sources"] == ["linkedin", "jobfound"],
          first.metadata_json)


def test_existing_state_untouched() -> None:
    print("existing row state untouched")
    s = fresh_session()
    job_meta = {"employment_type": "Full-time", "responsibilities": ["build things"]}
    row = seed(s, job_url="https://www.linkedin.com/jobs/view/4000000501/", company_name="Wingtip Toys",
               job_title="Backend Engineer", location="Springfield", applied=True, should_apply=True,
               metadata_json=json.dumps(job_meta), module2_attempted=True, module2_enriched_at=NOW - timedelta(days=2),
               module2_model="ollama-test", module3_fit_score=8, module3_fit_reasoning="fits",
               module3_resume_pdf_path="/tmp/r.pdf", module4_last_status="submitted",
               module4_submitted_at=NOW - timedelta(days=1), module4_attempts=1, module4_outcome="submitted")
    cols = [c.name for c in JobModel.__table__.columns if c.name not in ("last_seen_at", "metadata_json")]
    before = {c: getattr(row, c) for c in cols}
    insert_candidates(s, [JobCandidate(job_url="https://in.linkedin.com/jobs/view/be-4000000501", source="jobfound",
                                       company_name="Wingtip", job_title="Other", enrichment={"x": 1})], now=NOW)
    s.expire_all()
    row = s.get(JobModel, row.id)
    after = {c: getattr(row, c) for c in cols}
    check("every other column unchanged", before == after,
          str({c: (before[c], after[c]) for c in cols if before[c] != after[c]}))
    check("last_seen_at updated", row.last_seen_at == NOW, str(row.last_seen_at))
    m = json.loads(row.metadata_json)
    check("job keys kept, _sources added", {k: v for k, v in m.items() if k != "_sources"} == job_meta
          and m["_sources"] == ["linkedin", "jobfound"], row.metadata_json)
    insert_candidates(s, [JobCandidate(job_url="https://www.linkedin.com/jobs/view/4000000501/", source="jobfound")],
                      now=NOW)
    check("source not appended twice", meta(s, row.id)["_sources"] == ["linkedin", "jobfound"])

    odd = seed(s, job_url="https://www.linkedin.com/jobs/view/4000000502/", metadata_json="[1, 2]")
    r = insert_candidates(s, [JobCandidate(job_url="https://www.linkedin.com/jobs/view/4000000502/",
                                           source="jobfound")], now=NOW)
    s.expire_all()
    odd = s.get(JobModel, odd.id)
    check("non-object metadata not rewritten (counted)", odd.metadata_json == "[1, 2]" and r.seen_in_skipped == 1,
          f"{odd.metadata_json} {r}")
    check("non-object metadata row still gets last_seen_at", odd.last_seen_at == NOW)


def test_new_rows() -> None:
    print("pre-enriched vs bare-link insert")
    s = fresh_session()
    enr = {"employment_type": "Full-time", "description_full": "Build pipelines.", "_facts": {"job_type": "Full-time"}}
    r = insert_candidates(s, [
        JobCandidate(job_url="https://careers.proseware.test/jobs/5?utm_source=x", source="jobfound",
                     company_name="Proseware", job_title="Data Engineer", location="Springfield",
                     enrichment=enr, enrichment_model="jobfound", enrichment_error="thin description"),
        JobCandidate(job_url="https://www.linkedin.com/comm/jobs/view/4000000601?trk=x", source="linkedin"),
    ], now=NOW)
    pre, bare = (s.get(JobModel, i) for i in r.inserted_ids)
    pm = json.loads(pre.metadata_json)
    check("pre-enriched columns", pre.module2_attempted and pre.module2_enriched_at == NOW
          and pre.module2_model == "jobfound" and pre.module2_last_error == "thin description"
          and pre.source_platform == "jobfound" and pre.company_name == "Proseware" and pre.location == "Springfield")
    check("pre-enriched metadata = enrichment + _sources",
          pm == {**enr, "_sources": ["jobfound"]}, pre.metadata_json)
    check("stored url normalized (tracking dropped)", pre.job_url == "https://careers.proseware.test/jobs/5",
          pre.job_url)
    check("bare link un-enriched", not bare.module2_attempted and bare.module2_enriched_at is None
          and bare.module2_model is None and json.loads(bare.metadata_json) == {"_sources": ["linkedin"]})
    check("bare LinkedIn url canonical", bare.job_url == "https://www.linkedin.com/jobs/view/4000000601/", bare.job_url)
    check("first/last seen = now", pre.first_seen_at == NOW and bare.last_seen_at == NOW)
    r = insert_candidates(s, [JobCandidate(job_url="mailto:x@example.test", source="jobfound")], now=NOW)
    check("unusable url -> error, no row", r.errors and r.inserted == 0 and s.query(JobModel).count() == 2, str(r))


def test_module1_path() -> None:
    print("module 1 routed through insert_candidates")
    s = fresh_session()
    sender = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    emails = [
        RawEmail(message_id="m1", sender=sender, subject="alert one", date="", links=[
            "https://www.linkedin.com/comm/jobs/view/4000000701?trackingId=a",
            "https://www.linkedin.com/comm/jobs/view/4000000702?trackingId=b",
        ]),
        RawEmail(message_id="m2", sender=sender, subject="alert two", date="", links=[
            "https://www.linkedin.com/jobs/view/4000000701/?refId=c",
        ]),
    ]
    real_fetch = m1_runner.fetch_emails
    m1_runner.fetch_emails = lambda hours: emails
    try:
        res = m1_runner.run(s, hours=24)
        check("collector counts new/duplicates", (res.jobs_extracted, res.new_jobs, res.duplicate_jobs) == (3, 2, 1),
              f"{res.jobs_extracted} {res.new_jobs} {res.duplicate_jobs} {res.errors}")
        check("one row per job", s.query(JobModel).count() == 2)
        res = m1_runner.run(s, hours=24)
        check("rerun: all duplicates", (res.new_jobs, res.duplicate_jobs) == (0, 3) and s.query(JobModel).count() == 2)
        rows = s.query(JobModel).order_by(JobModel.id).all()
        check("rows linkedin, un-enriched", all(r.source_platform == "linkedin" and not r.module2_attempted
                                                 for r in rows))
        check("seen-in not duplicated", all(json.loads(r.metadata_json)["_sources"] == ["linkedin"] for r in rows))
    finally:
        m1_runner.fetch_emails = real_fetch


def test_enrichment_keeps_sources() -> None:
    print("module 2 save_enrichment_result keeps _sources")
    s = fresh_session()
    row = seed(s, job_url="https://www.linkedin.com/jobs/view/4000000801/",
               metadata_json=json.dumps({"_sources": ["linkedin", "jobfound"], "stale_job_key": 1}))
    save_enrichment_result(s, row, enrichment=EnrichmentOutput(
        company_name="Alpine Ski House", job_title="Data Engineer",
        job_metadata={"employment_type": "Full-time"}), model_name="ollama-test")
    m = meta(s, row.id)
    check("_sources survives, job keys replaced",
          m == {"employment_type": "Full-time", "_sources": ["linkedin", "jobfound"]}, str(m))
    bad = seed(s, job_url="https://www.linkedin.com/jobs/view/4000000802/", metadata_json="not json")
    save_enrichment_result(s, bad, enrichment=EnrichmentOutput(job_metadata={"k": "v"}), model_name="m")
    check("invalid old metadata -> plain replace", meta(s, bad.id) == {"k": "v"})


def main() -> int:
    for t in (test_url_and_linkedin_id, test_company_title_window, test_in_batch_and_dry_run,
              test_existing_state_untouched, test_new_rows, test_module1_path, test_enrichment_keeps_sources):
        t()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
