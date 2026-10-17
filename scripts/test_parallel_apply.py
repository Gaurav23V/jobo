#!/usr/bin/env python3
"""Parallel apply (GHO-5): the shared queue and the workers, offline.

Covers the hazards that matter when one LinkedIn worker and several form
workers apply at once (module4/apply_queue.py, module4/parallel.py):

  [1] concurrent claims: 4 threads race for 60 jobs; each job is claimed once
  [2] exclusivity: one in-flight job per employer and per ATS family
  [3] release vs finish: an untouched claim goes back, a started one never does
  [4] re-route from a form worker happens at most once
  [5] emailed codes: a mail consumed by one job is invisible to every other
  [6] ATS family mapping for URLs and mail senders
  [7] full run, threads standing in for processes: run_module4 with a
      LinkedIn worker + 3 form workers over a temp jobs DB, stubbed browser /
      router / agent. Every job ends in exactly one terminal state, form jobs
      are applied by form workers on the employer URL, Easy Apply by the
      LinkedIn worker, nothing is applied twice, agents really overlap
  [8] budget stop mid-run: every worker stops, no job is lost or double-run
  [9] a form worker that hits LinkedIn hands the job back once, uncharged
 [11] agents that keep seeing a sign-in wall on the signed-in browser stop
      the run after MAX_CONSECUTIVE_SIGN_IN_MISREADS, nobody charged (GHO-13)
 [10] the router against a REAL headless Chromium (the harness's own, never
      9333): it routes a local listing whose Apply opens the employer in a
      new tab, then the runner's tab cleanup runs at once, ten times over.
      The browser must stay alive with a page every time. On 2026-10-14 the
      first router closed its own tab and the cleanup then closed the last
      live one, so Chromium exited (and its relaunch dropped the session).

No network, no model; [10] uses the harness's isolated headless Chromium and a
local page. Writes only under TMPDIR.

    uv run python scripts/test_parallel_apply.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["JOBO_APPLY_DELAY_SEC"] = "0"
os.environ["JOBO_APPLY_JITTER_SEC"] = "0"
os.environ["JOBO_LINKEDIN_SESSION_CHECK"] = "0"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import (  # noqa: E402
    agent_driver,
    apply_queue,
    browser_session,
    budget_guard,
    linkedin_router,
    parallel,
    preflight,
    runner,
    submission_audit,
    upload_names,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' - ' + extra) if extra and not ok else ''}")
    if not ok:
        FAILURES.append(name)


TMP = Path(tempfile.mkdtemp(prefix="gho5_"))


def new_queue(tag: str) -> apply_queue.ApplyQueue:
    return apply_queue.ApplyQueue(f"test_{tag}", path=TMP / f"queue_{tag}.sqlite")


# --------------------------------------------------------------------------
def t1_concurrent_claims() -> None:
    print("[1] concurrent claims")
    q = new_queue("claims")
    q.create_run([(i, f"Company {i}") for i in range(1, 61)])
    for i in range(1, 61):
        q.set_route(i, "form", f"https://site{i}.example.com/apply")
    got: dict[str, list[int]] = {f"w{n}": [] for n in range(4)}

    def grab(name: str) -> None:
        while True:
            c = q.claim_route(name, "form")
            if c is None:
                return
            got[name].append(c.job_id)
            q.finish(c.job_id, name)

    threads = [threading.Thread(target=grab, args=(n,)) for n in got]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    every = [j for ids in got.values() for j in ids]
    check("all 60 jobs claimed", sorted(every) == list(range(1, 61)), f"{len(every)} claims")
    check("no job claimed twice", len(every) == len(set(every)))
    check("work spread over workers", sum(1 for ids in got.values() if ids) >= 2)


def t2_exclusivity() -> None:
    print("[2] employer and ATS-family exclusivity")
    q = new_queue("excl")
    q.create_run([(1, "Northwind"), (2, "Northwind in India"), (3, "Treyco"), (4, "Humongo"), (5, "Other")])
    for w in ("w1", "w2", "w3", "w3b"):
        q.register_worker(w, "form", None, None)
    q.set_route(1, "form", "https://northwind.wd3.myworkdayjobs.com/a")
    q.set_route(2, "form", "https://www.northwind.com/careers/b")
    q.set_route(3, "form", "https://treyco.wd1.myworkdayjobs.com/c")
    q.set_route(4, "form", "https://careers.humongo.com/d")
    q.set_route(5, "form", "https://jobs.other.co.in/e")
    a = q.claim_route("w1", "form")
    check("first claim takes the top job", a is not None and a.job_id == 1)
    b = q.claim_route("w2", "form")
    check("same employer and same Workday family both skipped", b is not None and b.job_id == 4, f"got {b}")
    c = q.claim_route("w3", "form")
    check("a third, unrelated job is free", c is not None and c.job_id == 5, f"got {c}")
    d = q.claim_route("w3b", "form")
    check("nothing else claimable while 1/4/5 are in flight", d is None, f"got {d}")
    q.finish(1, "w1")
    e = q.claim_route("w1", "form")
    check("after the Northwind job ends, the next one is claimable", e is not None and e.job_id in (2, 3))
    q2 = new_queue("excl_dead")
    q2.create_run([(1, "Humongo"), (2, "Humongo")])
    for w in ("a", "b"):
        q2.register_worker(w, "form", None, None)
    q2.set_route(1, "form", "https://careers.humongo.com/1")
    q2.set_route(2, "form", "https://careers.humongo.com/2")
    q2.claim_route("a", "form")
    check("same employer blocked while its worker lives", q2.claim_route("b", "form") is None)
    q2.worker_done("a")  # a died holding job 1
    other = q2.claim_route("b", "form")
    check("a dead worker's claim no longer blocks", other is not None and other.job_id == 2, f"got {other}")


def t3_release_finish() -> None:
    print("[3] release vs finish")
    q = new_queue("release")
    q.create_run([(1, "A"), (2, "B")])
    q.set_route(1, "form", "https://a.example.com")
    q.set_route(2, "form", "https://b.example.com")
    c1 = q.claim_route("w", "form")
    q.release(c1.job_id, "w")
    rows = {r["job_id"]: r for r in q.rows()}
    check("untouched claim goes back to pending", rows[1]["state"] == "pending")
    c1 = q.claim_route("w", "form")
    q.mark_agent_started(c1.job_id)
    q.release(c1.job_id, "w")
    rows = {r["job_id"]: r for r in q.rows()}
    check("a started claim is never re-queued", rows[1]["state"] == "done")
    q.finish(1, "intruder")
    check("finish by a non-owner is ignored", {r["job_id"]: r for r in q.rows()}[2]["state"] == "pending")


def t4_reroute_once() -> None:
    print("[4] re-route at most once")
    q = new_queue("reroute")
    q.create_run([(1, "A")])
    q.set_route(1, "form", "https://a.example.com")
    q.claim_route("f1", "form")
    check("first hand-back accepted", q.reroute_to_linkedin(1, "needs LinkedIn"))
    c = q.claim_route("li", "linkedin")
    check("LinkedIn worker can claim it", c is not None and c.job_id == 1)
    check("second hand-back refused", not q.reroute_to_linkedin(1, "again"))


def t5_otp_ledger() -> None:
    print("[5] emailed-code ledger")
    q = new_queue("otp")
    check("job 10 takes mail m1", q.consume_otp("m1", 10))
    check("job 11 cannot take m1", not q.consume_otp("m1", 11))
    check("job 10 may re-read its own m1", q.consume_otp("m1", 10))
    check("m1 hidden from job 11's search", "m1" in q.otp_consumed_keys(exclude_job=11))
    check("m1 not hidden from job 10's search", "m1" not in q.otp_consumed_keys(exclude_job=10))


def t6_families() -> None:
    print("[6] ATS families")
    f = apply_queue.family_for_url
    s = apply_queue.family_for_sender
    check("two Workday tenants share a family", f("https://a.wd3.myworkdayjobs.com/x") == f("https://b.wd1.myworkdayjobs.com/y") == "workday")
    check("Workday mail maps to Workday", s("Northwind <northwind@myworkday.com>") == "workday")
    check("Greenhouse board and mail match", f("https://boards.greenhouse.io/x") == s("no-reply@us.greenhouse-mail.io") == "greenhouse")
    check("plain employer domain is its registrable domain", f("https://careers.humongo.com/a") == "humongo.com")
    check("co.in handled", f("https://jobs.poplar.co.in/a") == "poplar.co.in")
    check("company key folds suffixes", apply_queue.company_key("Northwind in India") == apply_queue.company_key("Northwind"))


# --------------------------------------------------------------------------
class Stubs:
    """Replace everything that would touch a browser, the network or a model."""

    def __init__(self, routes: dict[int, linkedin_router.Route], agent_outcome=None):
        self.routes = routes
        self.calls: list[tuple[str, int, str]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.agent_outcome = agent_outcome or (lambda jid, url: agent_driver.ApplyOutcome(status="submitted", detail="ok"))
        self.saved = []

    def _set(self, mod, name, value):
        self.saved.append((mod, name, getattr(mod, name)))
        setattr(mod, name, value)

    def __enter__(self):
        self._set(browser_session, "ensure_browser_running", lambda *a, **k: "http://127.0.0.1:1")
        self._set(browser_session, "ensure_healthy", lambda *a, **k: "http://127.0.0.1:1")
        self._set(browser_session, "close_extra_tabs", lambda *a, **k: None)
        self._set(preflight, "check_job_open", lambda url, **k: preflight.PreflightResult(closed=False, reason="stub"))
        self._set(linkedin_router, "resolve", lambda cdp, url: self.routes[int(url.rstrip("/").rsplit("/", 1)[-1])])
        self._set(submission_audit, "write_audit_record", lambda **k: None)
        self._set(upload_names, "prepare", lambda jid, path, kind: path)
        self._set(budget_guard, "write_resume_marker", lambda *a, **k: None)

        def agent(**kw):
            name = threading.current_thread().name
            with self.lock:
                self.calls.append((name, kw["job_id"], kw["source_url"]))
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.15)
            with self.lock:
                self.active -= 1
            return self.agent_outcome(kw["job_id"], kw["source_url"])

        self._set(agent_driver, "run_apply_agent", agent)
        return self

    def __exit__(self, *exc):
        for mod, name, value in reversed(self.saved):
            setattr(mod, name, value)
        return False


def make_db(tag: str, n: int):
    path = TMP / f"jobs_{tag}.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    resume = TMP / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 stub")
    with Session() as s:
        for i in range(1, n + 1):
            s.add(JobModel(
                id=i, company_name=f"Employer {i}", job_title="Engineer",
                job_url=f"https://www.linkedin.com/jobs/view/{i}/",
                should_apply=True, applied=False, module3_fit_score=4,
                module3_resume_pdf_path=str(resume), module4_attempts=0,
            ))
        s.commit()
    return Session


def run_workers(tag: str, Session, n_jobs: int, form_workers: int, budget_check=None):
    q = new_queue(tag)
    q.create_run([(i, f"Employer {i}") for i in range(1, n_jobs + 1)])
    workers = [parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=form_workers, poll_sec=0.02)]
    workers += [parallel.Worker(f"f{n}", parallel.ROLE_FORM, q, form_workers=form_workers, poll_sec=0.02)
                for n in range(1, form_workers + 1)]
    for w in workers:
        q.register_worker(w.name, w.role, None, None)
    results = {}

    def go(w):
        with Session() as s:
            results[w.name] = runner.run_module4(
                s, worker=w, budget_check=budget_check or (lambda: budget_guard.decide(None, 1.0)))

    threads = [threading.Thread(target=go, args=(w,), name=w.name) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    check(f"[{tag}] every worker finished", not any(t.is_alive() for t in threads))
    return q, results


def routes_for(n: int) -> dict[int, linkedin_router.Route]:
    routes = {}
    for i in range(1, n + 1):
        if i % 5 == 0:
            routes[i] = linkedin_router.Route(linkedin_router.EASY_APPLY, reason="easy")
        elif i == 7:
            routes[i] = linkedin_router.Route(linkedin_router.CLOSED, reason="closed")
        elif i == 9:
            routes[i] = linkedin_router.Route(linkedin_router.UNKNOWN, reason="no button")
        else:
            routes[i] = linkedin_router.Route(linkedin_router.EXTERNAL, f"https://jobs.employer{i}.com/apply/{i}", "ext")
    return routes


def t7_full_run() -> None:
    print("[7] full parallel run (threads for processes)")
    n = 20
    Session = make_db("full", n)
    with Stubs(routes_for(n)) as st:
        q, results = run_workers("full", Session, n, form_workers=3)
    applied = [jid for _, jid, _ in st.calls]
    check("no job reached an agent twice", len(applied) == len(set(applied)), str(sorted(applied)))
    check("every open job reached an agent once", sorted(applied) == [i for i in range(1, n + 1) if i != 7])
    by_job = {jid: (who, url) for who, jid, url in st.calls}
    form_jobs = [i for i in range(1, n + 1) if i % 5 and i not in (7, 9)]
    check("employer-form jobs went to form workers", all(by_job[i][0].startswith("f") for i in form_jobs),
          str({i: by_job[i][0] for i in form_jobs}))
    check("form workers were given the employer URL", all(by_job[i][1] == f"https://jobs.employer{i}.com/apply/{i}" for i in form_jobs))
    check("Easy Apply and unroutable stayed with the LinkedIn worker",
          all(by_job[i][0] == "li" for i in (5, 9, 10, 15, 20)), str({i: by_job[i] for i in (5, 9, 10, 15, 20)}))
    check("LinkedIn worker gave Easy Apply the listing URL", by_job[5][1] == "https://www.linkedin.com/jobs/view/5/")
    check("agents ran in parallel", st.max_active >= 2, f"max_active={st.max_active}")
    with Session() as s:
        rows = {j.id: j for j in s.query(JobModel)}
    check("all open jobs applied in the DB", all(rows[i].applied for i in range(1, n + 1) if i != 7))
    check("closed listing marked closed, not applied", rows[7].module4_closed_at is not None and not rows[7].applied)
    total = parallel._merge({k: parallel.result_to_json(v) for k, v in results.items()})
    check("merged submitted count", total.submitted == n - 1, str(total.submitted))
    check("merged routing counts", total.routed_form == len(form_jobs) and total.routed_linkedin == 5,
          f"form={total.routed_form} li={total.routed_linkedin}")
    check("queue fully done", all(r["state"] == "done" for r in q.rows()), str(q.counts()))


def t8_budget_stop() -> None:
    print("[8] budget stop mid-run")
    n = 24
    Session = make_db("budget", n)
    calls = {"n": 0}
    lock = threading.Lock()

    def budget():
        with lock:
            calls["n"] += 1
            over = calls["n"] > 14
        return budget_guard.decide(0.80 if over else 0.50, 0.75)

    with Stubs(routes_for(n)) as st:
        q, results = run_workers("budget", Session, n, form_workers=3, budget_check=budget)
    applied = [jid for _, jid, _ in st.calls]
    check("stopped before the queue was done", len(applied) < n - 1, f"{len(applied)} applied")
    check("no job applied twice", len(applied) == len(set(applied)))
    check("every worker reported the budget stop or saw the stop flag",
          any(r.budget_exhausted for r in results.values()) and q.stop_reason() is not None)
    stuck = [r for r in q.rows() if r["state"] in ("claimed", "resolving")]
    check("no claim left dangling", not stuck, str(stuck))
    with Session() as s:
        applied_db = {j.id for j in s.query(JobModel).filter(JobModel.applied.is_(True))}
    check("DB applied set equals agent calls", applied_db == set(applied), f"{applied_db ^ set(applied)}")


def t9_reroute() -> None:
    print("[9] form worker hands a LinkedIn-only site back")
    n = 6
    Session = make_db("reroute", n)
    routes = {i: linkedin_router.Route(linkedin_router.EXTERNAL, f"https://jobs.e{i}.com/a", "ext") for i in range(1, n + 1)}

    def outcome(jid, url):
        if jid == 3 and "linkedin.com" not in url:
            return agent_driver.ApplyOutcome(status="linkedin_signed_out", detail="employer site requires LinkedIn")
        return agent_driver.ApplyOutcome(status="submitted", detail="ok")

    with Stubs(routes, agent_outcome=outcome) as st:
        q, results = run_workers("reroute", Session, n, form_workers=2)
    tries = [(who, url) for who, jid, url in st.calls if jid == 3]
    check("job 3 tried by a form worker, then by the LinkedIn worker",
          len(tries) == 2 and tries[0][0].startswith("f") and tries[1][0] == "li", str(tries))
    with Session() as s:
        j3 = s.get(JobModel, 3)
        check("no attempt charged for the hand-back", j3.applied and (j3.module4_attempts or 0) == 0,
              f"applied={j3.applied} attempts={j3.module4_attempts}")
    check("no LinkedIn sign-out recorded", not any(r.linkedin_signed_out for r in results.values()))


def t11_misread_breaker() -> None:
    print("[11] agents that cannot see the signed-in browser stop the run")
    from module4 import linkedin_session

    n = 8
    Session = make_db("misread", n)
    routes = {i: linkedin_router.Route(linkedin_router.EASY_APPLY, reason="easy") for i in range(1, n + 1)}
    # 15 Oct 2026: every agent ran in a blank local browser, so each one saw a
    # LinkedIn sign-in wall while our own probe of 9333 said signed in.
    wall = lambda jid, url: agent_driver.ApplyOutcome(status="linkedin_signed_out", detail="Sign in / Join now")  # noqa: E731
    saved = linkedin_session.confirm_signed_out
    linkedin_session.confirm_signed_out = lambda cdp: linkedin_session.SessionState("signed_in", "feed stayed")
    try:
        with Stubs(routes, agent_outcome=wall) as st:
            q, results = run_workers("misread", Session, n, form_workers=1)
    finally:
        linkedin_session.confirm_signed_out = saved
    stop_at = runner.MAX_CONSECUTIVE_SIGN_IN_MISREADS
    check(f"stopped after {stop_at} agents, not the whole queue", len(st.calls) == stop_at, str(len(st.calls)))
    check("run stop flag raised for every worker", bool(q.stop_reason()), str(q.stop_reason()))
    total = parallel._merge({k: parallel.result_to_json(v) for k, v in results.items()})
    check("mismatch reason reaches the merged result", "not driving this browser" in total.agent_browser_mismatch)
    v = runner.health_verdict(total)
    check("merged health DEGRADED with the reason", not v.healthy and any("sign-in wall" in r for r in v.reasons), str(v))
    with Session() as s:
        rows = list(s.query(JobModel))
    check("no job charged an attempt", all((j.module4_attempts or 0) == 0 for j in rows))
    # The streak's jobs go back exactly as they were (GHO-17), so every job
    # stays claimable, not only the ones no agent reached.
    check("every job keeps a clean status", all(not j.module4_last_status for j in rows),
          str([j.module4_last_status for j in rows]))


def t10_router_keeps_browser_alive() -> None:
    print("[10] router + tab cleanup on a real browser")
    import http.server
    import json
    import urllib.request

    if browser_session.cdp_port() == 9333:
        check("refusing to run against the production browser port 9333", False)
        return

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            if self.path.startswith("/closed/"):
                # Both of LinkedIn's closed banners (GHO-24), on a page that
                # still has a stray Apply link, like LinkedIn's "similar jobs".
                phrase = {"old": "No longer accepting applications",
                          "new": "Not currently accepting applications"}[self.path.rsplit("/", 1)[-1]]
                body = (f"<html><body><main><h1>Engineer</h1><span>{phrase}</span>"
                        f"<a href='/listing/9'>Apply</a></main></body></html>")
            elif self.path.startswith("/listing"):
                body = (f"<html><body><main><h1>Engineer</h1><button class='jobs-apply-button' "
                        f"onclick=\"window.open('http://localhost:{port}/employer/apply', '_blank')\">"
                        f"Apply</button></main></body></html>")
            else:
                body = "<html><body><h1>Employer application form</h1></body></html>"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    os.environ["JOBO_ROUTE_MIN_INTERVAL_SEC"] = "0"
    cdp = browser_session.ensure_healthy(None)
    check("isolated browser is up", bool(cdp))
    if not cdp:
        return

    def pages():
        with urllib.request.urlopen(f"{cdp}/json/list", timeout=3) as r:
            return [t for t in json.loads(r.read().decode()) if t.get("type") == "page"]

    try:
        for which, phrase in (("old", "No longer accepting applications"), ("new", "Not currently accepting applications")):
            r = linkedin_router.resolve(cdp, f"http://127.0.0.1:{port}/closed/{which}", _any_host=True)
            check(f"router calls the {which} closed banner closed", r.kind == linkedin_router.CLOSED and phrase in r.reason,
                  f"{r.kind}: {r.reason}")
        ok_routes, alive = 0, True
        for i in range(10):
            # Leftover tabs from "the previous job", like a real run has.
            for _ in range(i % 3):
                urllib.request.urlopen(urllib.request.Request(f"{cdp}/json/new?about:blank", method="PUT"), timeout=5)
            r = linkedin_router.resolve(cdp, f"http://127.0.0.1:{port}/listing/{i}", _any_host=True)
            if r.kind == linkedin_router.EXTERNAL and r.apply_url == f"http://localhost:{port}/employer/apply":
                ok_routes += 1
            browser_session.close_extra_tabs()
            browser_session.close_extra_tabs()
            if not browser_session._cdp_alive(browser_session.cdp_port()) or not pages():
                alive = False
                break
        check("router captured the employer URL every time", ok_routes == 10, f"{ok_routes}/10")
        check("browser alive with a page after every route + cleanup", alive)
        left = pages()
        check("cleanup left exactly one tab, on about:blank", len(left) == 1 and left[0].get("url") == "about:blank",
              str([t.get("url") for t in left]))
    finally:
        srv.shutdown()


def main() -> int:
    for t in (t1_concurrent_claims, t2_exclusivity, t3_release_finish, t4_reroute_once,
              t5_otp_ledger, t6_families, t7_full_run, t8_budget_stop, t9_reroute,
              t11_misread_breaker, t10_router_keeps_browser_alive):
        t()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS: parallel apply queue and workers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
