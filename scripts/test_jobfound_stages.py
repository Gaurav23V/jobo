"""Offline tests for GHO-41 part c: modules 2/3/4 with jobfound rows, and `jobo prefilter`.

  [1] module 2 never scrapes or calls Ollama for a jobfound row (LinkedIn or
      direct apply link), under default, --force and --job-id; LinkedIn email
      rows are still selected; a jobfound row's triage bundle has the posting
  [2] prefilter: rules fire only on known inputs; scored, queued, applied,
      closed and tried rows untouched; --dry-run writes nothing; dropped rows
      never reach module 3 (default, --job-id) or module 4; funnel counts them
  [3] module 4 routing helpers: ATS kind, initial route, circuit-breaker keys
      on shared ATS hosts
  [4] single-worker runner over LinkedIn, Greenhouse, Workday, iCIMS, Ashby
      and Rippling rows (stubbed browser, router, session, agent): which gates
      run, the URL handed to the agent, Rippling parked, a signed-out LinkedIn
      session stops LinkedIn jobs only
  [5] parallel run: direct links go straight to a form worker, the LinkedIn
      worker routes only LinkedIn listings
  [6] `jobo apply --plan` (module4/plan.py) reports the same decisions and
      touches nothing

Fictional employers and job ids, temp SQLite files only; nothing reaches
data/jobs.db, a browser, the network or a model.

Run: bash scripts/run_regression_tests.sh test_jobfound_stages
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
_TMP = Path(tempfile.mkdtemp(prefix="jobo-gho41c-test-"))
os.environ["JOBO_DB_PATH"] = str(_TMP / "unused.db")  # nothing may reach data/jobs.db
os.environ["JOBO_APPLY_DELAY_SEC"] = "0"
os.environ["JOBO_APPLY_JITTER_SEC"] = "0"
os.environ["JOBO_BLOCKED_EMPLOYERS"] = "Blockco"
os.environ.pop("JOBO_PAUSED_SITES", None)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import module2.linkedin_fetch as m2_fetch  # noqa: E402
import module2.ollama_client as m2_ollama  # noqa: E402
import module2.runner as m2_runner  # noqa: E402
from db.models import Base, JobModel  # noqa: E402
from module2.query import list_jobs_for_enrichment  # noqa: E402
from module3 import persist as m3_persist  # noqa: E402
from module3.job_bundle import build_job_bundle_text  # noqa: E402
from module3.prefilter import run_prefilter  # noqa: E402
from module3.query import list_jobs_for_module3  # noqa: E402
from module4 import (  # noqa: E402
    agent_driver,
    apply_queue,
    browser_session,
    budget_guard,
    circuit_breaker,
    linkedin_router,
    linkedin_session,
    parallel,
    plan,
    preflight,
    routing,
    runner,
    submission_audit,
    upload_names,
)
from module4.funnel import compute_funnel  # noqa: E402
from module4.query import list_jobs_to_apply  # noqa: E402
from policy import paused_sites  # noqa: E402

PASS = FAIL = 0
NOW = datetime(2026, 10, 15, 9, 0, 0)
RESUME = _TMP / "resume.pdf"
RESUME.write_bytes(b"%PDF-1.4 stub")

LI = "https://www.linkedin.com/jobs/view/4000000{:03d}/"
GH = "https://job-boards.greenhouse.io/northwindlabs/jobs/7000001"
WD = "https://treyco.wd5.myworkdayjobs.com/en-US/careers/job/Springfield/Data-Engineer_R-1001"
IC = "https://careers-humongo.icims.com/jobs/2001/data-analyst/job"
AS = "https://jobs.ashbyhq.com/poplarai/3f2a9c1e-0000-4000-8000-000000000001"
RP = "https://ats.rippling.com/contosolabs/jobs/9a8b7c6d"
GF = "https://docs.google.com/forms/d/e/1FAIpQLSfakeformid0001/viewform"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


_n = 0


def fresh_db():
    global _n
    _n += 1
    engine = create_engine(f"sqlite:///{_TMP / f'jobs{_n}.db'}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def jobfound_meta(facts: dict | None = None, description: str = "Build data pipelines in Python.") -> str:
    meta = {
        "employment_type": "Full-time",
        "description_full": description,
        "_sources": ["jobfound"],
        "_jobfound": {"id": "deadbeef", "page_url": "https://jobfound.org/job/deadbeef"},
    }
    if facts is not None:
        meta["_facts"] = {"job_type": None, "categories": [], "salary": None, "experience": None, **facts}
    return json.dumps(meta)


def seed(Session, **kw) -> int:
    for key, value in (("applied", False), ("referred_json", "[]"), ("source_platform", "linkedin"),
                       ("metadata_json", "{}"), ("module4_attempts", 0), ("first_seen_at", NOW),
                       ("last_seen_at", NOW)):
        kw.setdefault(key, value)
    with Session() as s:
        row = JobModel(**kw)
        s.add(row)
        s.commit()
        return row.id


def enriched(**kw) -> dict:
    kw.setdefault("module2_attempted", True)
    kw.setdefault("module2_enriched_at", NOW)
    return kw


def jobfound(**kw) -> dict:
    kw.setdefault("source_platform", "jobfound")
    kw.setdefault("module2_model", "jobfound")
    kw.setdefault("metadata_json", jobfound_meta({"categories": ["Data & AI"]}))
    return enriched(**kw)


def queued(**kw) -> dict:
    kw.setdefault("should_apply", True)
    kw.setdefault("module3_fit_score", 4)
    kw.setdefault("module3_resume_pdf_path", str(RESUME))
    kw.setdefault("module3_cover_pdf_path", str(RESUME))
    return enriched(**kw)


class Patch:
    """Set module attributes for a block; restore them after."""

    def __init__(self):
        self.saved = []

    def set(self, mod, name, value):
        self.saved.append((mod, name, getattr(mod, name)))
        setattr(mod, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for mod, name, value in reversed(self.saved):
            setattr(mod, name, value)
        return False


def _boom(what):
    def f(*a, **k):
        raise AssertionError(f"{what} must not be called")
    return f


# --------------------------------------------------------------------------
def t1_module2() -> None:
    print("[1] module 2 never touches jobfound rows")
    Session = fresh_db()
    jf_li = seed(Session, **jobfound(job_url=LI.format(1), company_name="Northwind Labs",
                                     job_title="Data Engineer", location="Springfield"))
    jf_gh = seed(Session, **jobfound(job_url=GH, company_name="Northwind Labs", job_title="Backend Engineer",
                                     location="Remote", module2_attempted=True))
    # A jobfound row the source could not fully enrich (thin description) is
    # still never scraped: module2_attempted false, source jobfound.
    jf_thin = seed(Session, job_url=IC, source_platform="jobfound", module2_model="jobfound",
                   module2_attempted=False, company_name="Humongo", job_title="Data Analyst")
    li_new = seed(Session, job_url=LI.format(2), company_name="Treyco", job_title="Data Analyst")
    li_done = seed(Session, **enriched(job_url=LI.format(3), module2_model="llama3.2:3b",
                                       company_name="Treyco", job_title="ML Engineer"))
    with Session() as s:
        ids = lambda **k: [j.id for j in list_jobs_for_enrichment(s, **k)]  # noqa: E731
        check("default selects only the unattempted LinkedIn email row", ids() == [li_new], str(ids()))
        check("--force selects LinkedIn rows only", ids(force=True) == [li_new, li_done], str(ids(force=True)))
        for jid in (jf_li, jf_gh, jf_thin):
            check(f"--job-id {jid} (jobfound) returns nothing", ids(job_id=jid) == [])
            check(f"--job-id {jid} --force (jobfound) returns nothing", ids(job_id=jid, force=True) == [])
        check("--job-id on a LinkedIn row still works", ids(job_id=li_done, force=True) == [li_done])

    with Patch() as p:
        for mod in (m2_fetch, m2_runner):
            p.set(mod, "extract_linkedin_job", _boom("LinkedIn scraper"))
            p.set(mod, "playwright_browser_context", _boom("Playwright"))
        for mod in (m2_ollama, m2_runner):
            p.set(mod, "generate_json_enrichment_with_retry", _boom("Ollama"))
        with Session() as s:
            # Only jobfound rows are left un-enriched-looking: mark the email row done.
            s.get(JobModel, li_new).module2_attempted = True
            s.commit()
            try:
                runs = [
                    m2_runner.run_enrich(s),
                    m2_runner.run_enrich(s, force=False, job_id=jf_thin),
                    m2_runner.run_enrich(s, force=True, job_id=jf_li),
                    m2_runner.run_enrich(s, force=True, job_id=jf_gh),
                ]
                ok, err = all(r.attempted == 0 for r in runs), ""
            except AssertionError as e:
                ok, err = False, str(e)
            check("run_enrich default / --job-id / --force --job-id never reach scraper or Ollama", ok, err)
        # --force over a DB holding only jobfound rows: nothing to enrich.
        OnlyJf = fresh_db()
        only = seed(OnlyJf, **jobfound(job_url=GH, company_name="Northwind Labs", job_title="Backend Engineer"))
        seed(OnlyJf, **jobfound(job_url=LI.format(4), company_name="Poplar", job_title="Data Engineer"))
        with OnlyJf() as s:
            try:
                ok, err = m2_runner.run_enrich(s, force=True).attempted == 0, ""
            except AssertionError as e:
                ok, err = False, str(e)
            row = s.get(JobModel, only)
            check("run_enrich --force skips jobfound rows, metadata kept as the source wrote it",
                  ok and row.module2_model == "jobfound" and "_jobfound" in json.loads(row.metadata_json), err)

        with Session() as s:
            bundle = build_job_bundle_text(s.get(JobModel, jf_li))
        check("jobfound bundle has title, company, location, description, source",
              all(x in bundle for x in ("job_title: Data Engineer", "company_name: Northwind Labs",
                                        "location: Springfield", "Build data pipelines in Python.",
                                        "source_platform: jobfound")), bundle[:300])


# --------------------------------------------------------------------------
def t2_prefilter() -> None:
    print("[2] prefilter")
    Session = fresh_db()
    ids = {
        "li_analyst": seed(Session, **enriched(job_url=LI.format(10), job_title="Data Analyst",
                                               company_name="Treyco")),
        "li_intern": seed(Session, **enriched(job_url=LI.format(11), job_title="Software Engineer Intern",
                                              company_name="Treyco")),
        "jf_low": seed(Session, **jobfound(job_url=GH, job_title="Backend Engineer", company_name="Northwind Labs",
                                           metadata_json=jobfound_meta({"categories": ["Engineering and Development"],
                                                                        "salary": "8-11 LPA"}))),
        "jf_ok": seed(Session, **jobfound(job_url=LI.format(12), job_title="Backend Engineer", company_name="Poplar",
                                          metadata_json=jobfound_meta({"categories": ["Engineering and Development"],
                                                                       "salary": "10-12 LPA"}))),
        "scored": seed(Session, **enriched(job_url=LI.format(13), job_title="Data Intern", module3_fit_score=2,
                                           should_apply=False)),
        "queued": seed(Session, **queued(job_url=LI.format(14), job_title="Analytics Intern")),
        "applied": seed(Session, **enriched(job_url=LI.format(15), job_title="Intern", applied=True)),
        "closed": seed(Session, **enriched(job_url=LI.format(16), job_title="Intern",
                                           module4_closed_at=NOW)),
        "tried": seed(Session, **enriched(job_url=LI.format(17), job_title="Intern", module4_attempts=1)),
        "unenriched": seed(Session, job_url=LI.format(18), job_title="Intern"),
    }
    snapshot = lambda s: {  # noqa: E731
        k: (r.prefilter_reason, r.should_apply, r.module3_fit_reasoning, r.module3_fit_score)
        for k, r in ((k, s.get(JobModel, v)) for k, v in ids.items())
    }
    with Session() as s:
        before = snapshot(s)
        dry = run_prefilter(s, dry_run=True)
        s.expire_all()
        check("--dry-run writes nothing", snapshot(s) == before)
        check("dry run counts: 4 examined, 2 dropped", (dry.examined, dry.dropped, dry.kept) == (4, 2, 2),
              f"{dry.examined}/{dry.dropped}/{dry.kept}")
        res = run_prefilter(s)
        s.expire_all()
        after = snapshot(s)
    check("per reason", dict(res.by_reason) == {"internship": 1, "low_pay": 1}, str(dict(res.by_reason)))
    check("per source", dict(res.by_source) == {"linkedin": 1, "jobfound": 1}
          and dict(res.examined_by_source) == {"linkedin": 2, "jobfound": 2}, str(res.render(dry_run=False)))
    check("LinkedIn 'Data Analyst' without _facts kept", after["li_analyst"][0] is None and after["li_analyst"][1] is None)
    check("'Software Engineer Intern' dropped",
          after["li_intern"] == ("internship", False, "prefilter: internship", None), str(after["li_intern"]))
    check("jobfound 8-11 LPA dropped", after["jf_low"] == ("low_pay", False, "prefilter: low_pay", None),
          str(after["jf_low"]))
    check("jobfound 10-12 LPA kept", after["jf_ok"][0] is None)
    for k in ("scored", "queued", "applied", "closed", "tried", "unenriched"):
        check(f"{k} row untouched", after[k] == before[k], f"{before[k]} -> {after[k]}")
    with Session() as s:
        again = run_prefilter(s)
    check("second run examines only the kept rows", (again.examined, again.dropped) == (2, 0))

    with Session() as s:
        m3 = [j.id for j in list_jobs_for_module3(s)]
        check("module 3 default skips prefiltered rows",
              ids["li_intern"] not in m3 and ids["jf_low"] not in m3 and ids["li_analyst"] in m3, str(m3))
        check("module 3 --job-id skips a prefiltered row", list_jobs_for_module3(s, job_id=ids["li_intern"]) == [])
        check("module 3 --force skips prefiltered rows",
              ids["li_intern"] not in [j.id for j in list_jobs_for_module3(s, force=True)])
        check("module 3 --queued skips prefiltered rows",
              ids["li_intern"] not in [j.id for j in list_jobs_for_module3(s, queued=True)])
        forced = list_jobs_for_module3(s, force=True, job_id=ids["jf_low"])
        check("module 3 --force --job-id re-scores one prefiltered row", [j.id for j in forced] == [ids["jf_low"]])
        apply_ids = [j.id for j in list_jobs_to_apply(s, force=True)]
        check("module 4 queue never has prefiltered rows", ids["li_intern"] not in apply_ids
              and ids["jf_low"] not in apply_ids and ids["queued"] in apply_ids, str(apply_ids))
        # Backstop: a hand-edited should_apply=1 on a prefiltered row still stays out.
        s.get(JobModel, ids["li_intern"]).should_apply = True
        s.commit()
        check("module 4 backstop: prefilter_reason alone keeps a row out",
              list_jobs_to_apply(s, job_id=ids["li_intern"], force=True) == [])
        funnel = compute_funnel(s, since=NOW - timedelta(days=1), until=NOW + timedelta(days=1))
        check("funnel shows prefiltered counts per reason",
              funnel.prefiltered == {"internship": 1, "low_pay": 1}
              and "Prefiltered before triage (not in the rate): 2" in funnel.render("t"), str(funnel.prefiltered))
        # The --force --job-id override: a triage verdict clears the drop.
        row = s.get(JobModel, ids["jf_low"])
        m3_persist.apply_phase1_fit(s, row, fit_score=4, reasoning="good fit", highlighted_projects_json="[]",
                                    should_apply=True, dry_run=False)
        s.expire_all()
        row = s.get(JobModel, ids["jf_low"])
        check("a triage verdict on an overridden row clears prefilter_reason",
              row.prefilter_reason is None and row.should_apply is True)


# --------------------------------------------------------------------------
def t3_routing_helpers() -> None:
    print("[3] routing helpers")
    kinds = {LI.format(1): "linkedin", GH: "greenhouse", WD: "workday", IC: "icims", AS: "ashby",
             RP: "rippling", GF: "google_form", "https://careers.poplar.example/jobs/12": "other"}
    for url, kind in kinds.items():
        check(f"ats_kind {kind}", routing.ats_kind(url) == kind, routing.ats_kind(url))
    check("LinkedIn listing unrouted", routing.initial_route(LI.format(1))[0] is None)
    for url in (GH, WD, IC, AS, RP):
        check(f"direct {routing.ats_kind(url)} goes to a form worker with its own URL",
              routing.initial_route(url)[:2] == (apply_queue.ROUTE_FORM, url))
    check("direct Google Form stays on the signed-in browser",
          routing.initial_route(GF)[:2] == (apply_queue.ROUTE_LINKEDIN, GF))
    check("garbage URL left to the LinkedIn worker", routing.initial_route("not a url")[0] is None)
    d = circuit_breaker._domain
    check("breaker keys Greenhouse per employer",
          d(GH) == "job-boards.greenhouse.io/northwindlabs"
          and d("https://boards.greenhouse.io/otherco/jobs/1") == "boards.greenhouse.io/otherco")
    check("breaker keys Greenhouse embed by ?for=",
          d("https://boards.greenhouse.io/embed/job_app?for=Acmeco&token=1") == "boards.greenhouse.io/acmeco")
    check("breaker keys Ashby / Rippling per employer",
          d(AS) == "jobs.ashbyhq.com/poplarai" and d(RP) == "ats.rippling.com/contosolabs")
    check("breaker: shared host without an employer has no key", d("https://jobs.ashbyhq.com/") == "")
    check("breaker: per-tenant hosts unchanged",
          d(WD) == "treyco.wd5.myworkdayjobs.com" and d(IC) == "careers-humongo.icims.com")
    check("breaker: LinkedIn still never keyed", d(LI.format(1)) == "")


# --------------------------------------------------------------------------
class ApplyStubs(Patch):
    """Everything that would touch a browser, the network or a model."""

    def __init__(self, routes=None, session_state="signed_in"):
        super().__init__()
        self.routes = routes or {}
        self.session_state = session_state
        self.agent_calls: list[tuple[str, int, str, str]] = []
        self.preflight_calls: list[tuple[str, str]] = []
        self.resolve_calls: list[str] = []
        self.session_checks = 0
        self.lock = threading.Lock()

    def __enter__(self):
        self.set(browser_session, "ensure_browser_running", lambda *a, **k: "http://127.0.0.1:1")
        self.set(browser_session, "ensure_healthy", lambda *a, **k: "http://127.0.0.1:1")
        self.set(browser_session, "close_extra_tabs", lambda *a, **k: None)
        self.set(runner, "hermes_preflight", lambda result: True)
        self.set(submission_audit, "write_audit_record", lambda **k: None)
        self.set(upload_names, "prepare", lambda jid, path, kind: path)
        self.set(budget_guard, "write_resume_marker", lambda *a, **k: None)
        self.set(linkedin_session, "confirm_signed_out", _boom("feed probe"))
        real_preflight = preflight.check_job_open

        def pf(url, **k):
            with self.lock:
                if preflight._is_linkedin_job_url(url):
                    self.preflight_calls.append((url, "fetch"))
                    return preflight.PreflightResult(closed=False, reason="stub", checked=True)
                # The real function, which must answer without any fetch.
                r = real_preflight(url, **k)
                self.preflight_calls.append((url, r.reason))
                return r

        self.set(preflight, "check_job_open", pf)

        def resolve(cdp, url, **k):
            with self.lock:
                self.resolve_calls.append(url)
            return self.routes[url]

        self.set(linkedin_router, "resolve", resolve)

        def check_session(cdp, **k):
            with self.lock:
                self.session_checks += 1
            return linkedin_session.SessionState(self.session_state, "stub")

        self.set(linkedin_session, "check_session", check_session)

        def agent(**kw):
            with self.lock:
                self.agent_calls.append((threading.current_thread().name, kw["job_id"], kw["source_url"],
                                         kw["user_prompt"]))
            return agent_driver.ApplyOutcome(status="submitted", detail="ok")

        self.set(agent_driver, "run_apply_agent", agent)
        return self


def t4_single_worker() -> None:
    print("[4] single-worker runner on LinkedIn and direct ATS rows")
    os.environ["JOBO_LINKEDIN_SESSION_CHECK"] = "1"
    Session = fresh_db()
    rows = {
        "linkedin": seed(Session, **queued(job_url=LI.format(20), company_name="Treyco", module3_fit_score=5)),
        "greenhouse": seed(Session, **queued(job_url=GH, company_name="Northwind Labs", source_platform="jobfound")),
        "workday": seed(Session, **queued(job_url=WD, company_name="Treyco India", source_platform="jobfound")),
        "icims": seed(Session, **queued(job_url=IC, company_name="Humongo", source_platform="jobfound")),
        "ashby": seed(Session, **queued(job_url=AS, company_name="Poplar AI", source_platform="jobfound")),
        "rippling": seed(Session, **queued(job_url=RP, company_name="Contoso Labs", source_platform="jobfound")),
        "blocked": seed(Session, **queued(job_url="https://jobs.lever.co/blockco/1", company_name="Blockco",
                                          source_platform="jobfound")),
    }
    url_of = {k: u for k, u in (("linkedin", LI.format(20)), ("greenhouse", GH), ("workday", WD), ("icims", IC),
                                 ("ashby", AS), ("rippling", RP))}
    with ApplyStubs() as st, Session() as s:
        check("blocklisted employer is not in the apply queue",
              rows["blocked"] not in [j.id for j in list_jobs_to_apply(s)])
        result = runner.run_module4(s, budget_check=lambda: budget_guard.decide(None, 1.0))
    by_job = {jid: (url, prompt) for _, jid, url, prompt in st.agent_calls}
    for k in ("linkedin", "greenhouse", "workday", "icims", "ashby"):
        check(f"{k}: agent handed its job_url", by_job.get(rows[k], ("",))[0] == url_of[k], str(by_job.get(rows[k])))
    check("rippling: parked on the paused site, no agent", rows["rippling"] not in by_job and result.paused == 1)
    check("blocked employer never reached an agent", rows["blocked"] not in by_job)
    check("router never used by the single-worker runner", st.resolve_calls == [])
    check("LinkedIn session checked for the LinkedIn listing only", st.session_checks == 1, str(st.session_checks))
    pf = dict(st.preflight_calls)
    check("preflight fetch only for the LinkedIn listing", pf.get(url_of["linkedin"]) == "fetch"
          and all(pf.get(url_of[k]) == "not a LinkedIn job URL" for k in ("greenhouse", "workday", "icims", "ashby")),
          str(pf))
    check("no FORM WORKER note on the single worker", all("FORM WORKER" not in p for _, p in by_job.values()))
    with Session() as s:
        rp = s.get(JobModel, rows["rippling"])
        check("rippling row parked, uncharged", (rp.module4_last_status or "").startswith(paused_sites.STATUS_PREFIX)
              and rp.module4_attempts == 0 and not rp.applied, f"{rp.module4_last_status} {rp.module4_attempts}")
        check("direct rows submitted", all(s.get(JobModel, rows[k]).applied
                                           for k in ("greenhouse", "workday", "icims", "ashby")))

    # Signed out: a direct link still applies, the LinkedIn listing stops the stage.
    Session = fresh_db()
    gh = seed(Session, **queued(job_url=GH, company_name="Northwind Labs", module3_fit_score=5))
    li = seed(Session, **queued(job_url=LI.format(21), company_name="Treyco", module3_fit_score=4))
    with ApplyStubs(session_state="signed_out") as st, Session() as s:
        result = runner.run_module4(s, budget_check=lambda: budget_guard.decide(None, 1.0))
    applied = [jid for _, jid, _, _ in st.agent_calls]
    check("signed-out browser: the direct Greenhouse job still applied", applied == [gh], str(applied))
    check("signed-out browser: stopped at the LinkedIn listing, uncharged",
          result.linkedin_signed_out and li not in applied)


def t5_parallel() -> None:
    print("[5] parallel run: direct links skip LinkedIn routing")
    os.environ["JOBO_LINKEDIN_SESSION_CHECK"] = "0"
    Session = fresh_db()
    l_ext = seed(Session, **queued(job_url=LI.format(30), company_name="Treyco"))
    l_easy = seed(Session, **queued(job_url=LI.format(31), company_name="Fabrikam"))
    gh = seed(Session, **queued(job_url=GH, company_name="Northwind Labs", source_platform="jobfound"))
    wd = seed(Session, **queued(job_url=WD, company_name="Humongo", source_platform="jobfound"))
    gf = seed(Session, **queued(job_url=GF, company_name="Poplar", source_platform="jobfound"))
    employer = "https://careers.treyco.example/apply/30"
    routes = {
        LI.format(30): linkedin_router.Route(linkedin_router.EXTERNAL, employer, "ext"),
        LI.format(31): linkedin_router.Route(linkedin_router.EASY_APPLY, reason="easy"),
    }
    q = apply_queue.ApplyQueue("test_gho41c", path=_TMP / "queue.sqlite")
    with Session() as s:
        jobs = list_jobs_to_apply(s)
        q.create_run([(j.id, j.company_name) for j in jobs])
        pre = parallel.preroute(q, jobs)
    check("three direct links pre-routed", pre == 3, str(pre))
    workers = [parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=1, poll_sec=0.02),
               parallel.Worker("f1", parallel.ROLE_FORM, q, form_workers=1, poll_sec=0.02)]
    for w in workers:
        q.register_worker(w.name, w.role, None, None)

    with ApplyStubs(routes=routes) as st:
        def go(w):
            with Session() as s:
                runner.run_module4(s, worker=w, budget_check=lambda: budget_guard.decide(None, 1.0))

        threads = [threading.Thread(target=go, args=(w,), name=w.name) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    check("every worker finished", not any(t.is_alive() for t in threads))
    by_job = {jid: (who, url, prompt) for who, jid, url, prompt in st.agent_calls}
    check("each job reached an agent once", sorted(by_job) == sorted([l_ext, l_easy, gh, wd, gf])
          and len(st.agent_calls) == 5, str(sorted(by_job)))
    check("LinkedIn worker routed only the LinkedIn listings",
          sorted(st.resolve_calls) == sorted([LI.format(30), LI.format(31)]), str(st.resolve_calls))
    check("Greenhouse and Workday applied by the form worker on their own URL",
          by_job[gh][:2] == ("f1", GH) and by_job[wd][:2] == ("f1", WD), f"{by_job[gh][:2]} {by_job[wd][:2]}")
    check("direct link prompt says there is no LinkedIn listing",
          "direct link from the job source" in by_job[gh][2] and "LinkedIn listing (" not in by_job[gh][2])
    check("routed LinkedIn job applied by the form worker on the employer URL",
          by_job[l_ext][:2] == ("f1", employer) and f"LinkedIn listing ({LI.format(30)})" in by_job[l_ext][2])
    check("Easy Apply and the direct Google Form stayed on the LinkedIn worker",
          by_job[l_easy][:2] == ("li", LI.format(31)) and by_job[gf][:2] == ("li", GF),
          f"{by_job[l_easy][:2]} {by_job[gf][:2]}")


def t6_plan() -> None:
    print("[6] jobo apply --plan")
    Session = fresh_db()
    li = seed(Session, **queued(job_url=LI.format(40), company_name="Treyco", module3_fit_score=5))
    gh = seed(Session, **queued(job_url=GH, company_name="Northwind Labs", source_platform="jobfound"))
    rp = seed(Session, **queued(job_url=RP, company_name="Contoso Labs", source_platform="jobfound"))
    gf = seed(Session, **queued(job_url=GF, company_name="Poplar", source_platform="jobfound"))
    bl = seed(Session, **queued(job_url="https://jobs.lever.co/blockco/1", company_name="Blockco"))
    pf = seed(Session, **enriched(job_url=IC, company_name="Humongo", job_title="Data Intern",
                                  should_apply=False, prefilter_reason="internship",
                                  module3_fit_reasoning="prefilter: internship"))
    with Patch() as p, Session() as s:
        for mod, name in ((browser_session, "ensure_browser_running"), (browser_session, "ensure_healthy"),
                          (agent_driver, "run_apply_agent"), (linkedin_router, "resolve"),
                          (linkedin_session, "check_session"), (preflight, "check_job_open")):
            p.set(mod, name, _boom(f"{mod.__name__}.{name}"))
        try:
            rows = {r.job_id: r for r in plan.build_plan(s)}
            one_bl = plan.build_plan(s, job_id=bl)
            one_pf = plan.build_plan(s, job_id=pf)
            one_li = plan.build_plan(s, job_id=li)
            text = plan.render(list(rows.values()) + one_pf)
            ok = True
        except AssertionError as e:
            ok, rows, text = False, {}, str(e)
        dirty = bool(s.dirty or s.new)
    check("plan touched no browser, agent, router, session or preflight", ok, text)
    if not ok:
        return
    check("plan wrote nothing", not dirty)
    check("queued rows listed, blocked one left out", sorted(rows) == sorted([li, gh, rp, gf]), str(sorted(rows)))
    r = rows[li]
    check("LinkedIn row: preflight + session check, LinkedIn worker routes it",
          r.kind == "linkedin" and r.preflight and r.linkedin_session_check
          and r.worker == plan.WORKER_LINKEDIN_ROUTES and r.verdict == "apply")
    r = rows[gh]
    check("Greenhouse row: no preflight or session check, form worker direct",
          r.kind == "greenhouse" and not r.preflight and not r.linkedin_session_check
          and r.worker == plan.WORKER_FORM and r.verdict == "apply")
    check("Rippling row: paused site", rows[rp].kind == "rippling" and rows[rp].paused == "ats.rippling.com"
          and rows[rp].verdict == "skip (paused site)")
    check("Google Form row: LinkedIn worker direct", rows[gf].worker == plan.WORKER_LINKEDIN_DIRECT)
    check("--job-id on a blocked row explains it", len(one_bl) == 1 and one_bl[0].blocklisted
          and one_bl[0].verdict.startswith("NOT QUEUED: blocked employer"), str(one_bl and one_bl[0].verdict))
    check("--job-id on a prefiltered row explains it",
          len(one_pf) == 1 and one_pf[0].verdict == "NOT QUEUED: prefiltered (internship)",
          str(one_pf and one_pf[0].verdict))
    check("--job-id on a queued row reports it", len(one_li) == 1 and one_li[0].queued)
    check("render lists every row", all(f"job {j}:" in text for j in (li, gh, rp, gf, pf)), text)


def main() -> int:
    for t in (t1_module2, t2_prefilter, t3_routing_helpers, t4_single_worker, t5_parallel, t6_plan):
        t()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
