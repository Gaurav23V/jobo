#!/usr/bin/env python
"""hCaptcha sign-in walls are stopped fast and parked, not charged (GHO-54).

1878 (Oracle Cloud) and 1934 (iCIMS), 2026-10-18: both met an hCaptcha image
challenge on the email step in front of the application, spent 15 and 17
model steps on it, and were dropped as captcha_failed. A fresh scratch browser
met the same challenge on both sites, so it is the site, not our browser
(module4/captcha_walls.py). The fixtures in scripts/fixtures/hcaptcha/ are the
real page snapshots of those two runs with names, hosts and the email replaced.

GHO-73, 2026-10-22: 2197 (Paycom) and 2134 (Lever) met hCaptcha challenges the
snapshot never shows (Paycom's hidden challenge frame, Lever's page-sized
enclave iframe) and spent 33 and 72 steps. Both sites challenged every fresh
scratch profile, so they are site walls: parked on arrival, or before any
agent. paycom_guest_dialog.json and lever_apply_enclave.json have the shape of
those runs' results with employer names and ids replaced.

GHO-81, 2026-10-23: Cutshort's resume step hangs on Cloudflare Turnstile
(error 300030, save-resume 401) in the jobo browser; nine jobs, none submitted.
cutshort.io is a site wall too, with a Turnstile notice and park detail.

Offline: temporary SQLite DB, no browser, no model calls.

    uv run python scripts/test_hcaptcha_walls.py
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import agent_driver, blockers, budget_guard, captcha_walls, query, run_report, runner  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures") / "hcaptcha"
PLUGIN_DIR = REPO / "hermes_plugin" / "jobo-browser"
ORACLE = "contoso.fa.oraclecloud.com"
ICIMS = "careers-fabrikam.icims.com"
PAYCOM = "www.paycomonline.net"
LEVER = "jobs.lever.co"
CUTSHORT = "cutshort.io"
OTHER = "boards.greenhouse.io"  # any host outside captcha_walls

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_guard():
    print("\n[1] captcha_guard on the real snapshots")
    g = _load("jobo_hcaptcha_guard_test", PLUGIN_DIR / "captcha_guard.py")
    g.reset_state()
    oracle = _fixture("oracle_email_gate_challenge")
    seen = g.observe("browser_snapshot", json.dumps(oracle))
    check("Oracle challenge seen as hcaptcha", bool(seen) and seen[0] == "hcaptcha", str(seen and seen[0]))
    check("Oracle: host taken from the frame tree, ends as captcha_wall",
          bool(seen) and "STATUS: captcha_wall" in seen[1] and f"at {ORACLE}" in seen[1], seen and seen[1])
    check("vision blocked while the challenge is up", bool(g.check_tool_call("browser_vision", {"question": "tiles?"})))
    check("synthetic drags blocked too",
          bool(g.check_tool_call("browser_cdp", {"method": "Input.dispatchMouseEvent", "params": {}})))

    g.reset_state()
    before = _fixture("icims_login_before_next")
    check("iCIMS email step with only the invisible widget: no wall",
          g.observe("browser_snapshot", json.dumps(before)) is None)
    check("nothing blocked before the challenge", g.check_tool_call("browser_vision", {"question": "x"}) is None)
    after = _fixture("icims_login_challenge")
    after_click = {"success": True, "clicked": "e14", "snapshot": after["snapshot"]}  # no url, no frame tree
    seen = g.observe("browser_navigate", json.dumps(after_click))
    check("iCIMS drag challenge after Next: captcha_wall on the host seen last",
          bool(seen) and "STATUS: captcha_wall" in seen[1] and f"at {ICIMS}" in seen[1], seen and seen[1])
    g.observe("browser_snapshot", json.dumps(before))
    check("challenge gone: vision allowed again", g.check_tool_call("browser_vision", {"question": "x"}) is None)

    g.reset_state()
    other = dict(oracle, url=f"https://{OTHER}/example/jobs/123")
    seen = g.observe("browser_navigate", json.dumps(other))
    check("same challenge on a non-gate form: stop now, but as captcha_failed",
          bool(seen) and "STATUS: captcha_failed" in seen[1] and "captcha_wall" not in seen[1], seen and seen[1])

    check("plugin and module4 agree on the gate hosts",
          tuple(g.GATE_HOST_SUFFIXES) == tuple(captcha_walls.GATE_HOST_SUFFIXES))
    check("plugin and module4 agree on the site-wall hosts",
          tuple(g.SITE_WALL_HOST_SUFFIXES) == tuple(captcha_walls.SITE_WALL_HOST_SUFFIXES))

    print("\n[2] plugin: the notice survives the snapshot filter")
    plugin = _load("jobo_hcaptcha_plugin_test", PLUGIN_DIR / "__init__.py")
    plugin.captcha_guard.reset_state()
    out = json.loads(plugin._transform_tool_result("browser_snapshot", {}, json.dumps(oracle)))
    check("notice at the top of the filtered snapshot",
          str(out.get("snapshot", "")).startswith("[jobo: an hCaptcha image challenge"), str(out)[:200])
    check("result tagged with the wall kind", out.get("jobo_captcha_wall") == "hcaptcha")


def test_verdict_and_rules():
    print("\n[3] verdict, retry rules, run-report signature")
    v = agent_driver.parse_verdict(
        f"STATUS: captcha_wall\nDETAIL: hCaptcha challenge on the email/sign-in step at {ORACLE}\nFIELDS_FILLED_JSON: {{}}"
    )
    check("captcha_wall is a valid agent status", v.status == "captcha_wall", v.status)
    other = agent_driver.parse_verdict(f"STATUS: captcha_wall\nDETAIL: hCaptcha challenge at {OTHER}\n")
    check("captcha_wall on a non-gate host is read as captcha_failed", other.status == "captcha_failed", other.status)
    check("a non-gate status never counts as parked",
          not captcha_walls.is_parked(f"captcha_wall:hCaptcha challenge on {OTHER}"))
    parked = captcha_walls.status_for(ORACLE, v.detail)
    check("parked status is recognised", captcha_walls.is_parked(parked))
    check("a parked status is never read back as a permanent drop",
          blockers.permanent_reason_from_last_status(parked) is None)
    check("captcha_wall naming no gate host is dropped like captcha_failed",
          blockers.permanent_reason("captcha_wall", f"hCaptcha at {OTHER}") is not None)
    check("1878's real status is still a captcha:hcaptcha failure",
          run_report.signature_for("captcha_failed:Oracle Cloud application gated by hCaptcha image challenge") == "captcha:hcaptcha")
    sig = run_report.signature_for(parked)
    check("a parked wall reports as captcha:wall_parked / needs_g",
          sig == "captcha:wall_parked" and run_report.suggested_bucket(sig) == "needs_g", sig)
    check("LinkedIn job URLs never mark a host as walled",
          captcha_walls.own_host("https://www.linkedin.com/jobs/view/1/") == "")


def _session():
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(s, jid, url, *, attempts=0, last=None):
    pdf = Path(tempfile.gettempdir()) / "jobo_hcaptcha_resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    j = JobModel(id=jid, company_name="Contoso", job_title="Software Engineer II", job_url=url,
                 should_apply=True, applied=False, module2_enriched_at=datetime.datetime.now(),
                 module3_resume_pdf_path=str(pdf), module4_attempts=attempts, module4_last_status=last)
    s.add(j)
    s.commit()
    return j


def _run(s, outcome, **kw):
    spawned = []

    def fake_agent(**k):
        spawned.append(k["job_id"])
        return outcome

    with ExitStack() as st:
        st.enter_context(patch.object(runner.browser_session, "ensure_browser_running", return_value="http://127.0.0.1:1"))
        st.enter_context(patch.object(runner.browser_session, "ensure_healthy", return_value="http://127.0.0.1:1"))
        st.enter_context(patch.object(runner.browser_session, "close_extra_tabs"))
        st.enter_context(patch.object(runner.preflight, "check_job_open", return_value=SimpleNamespace(should_skip=False)))
        st.enter_context(patch.object(runner.agent_driver, "run_apply_agent", side_effect=fake_agent))
        st.enter_context(patch.object(runner.submission_audit, "write_audit_record"))
        st.enter_context(patch.object(runner.circuit_breaker, "is_tripped", return_value=None))
        st.enter_context(patch.object(runner.circuit_breaker, "record_failure"))
        st.enter_context(patch.object(runner.linkedin_session, "check_session",
                                      return_value=runner.linkedin_session.SessionState("signed_in", "test")))
        st.enter_context(patch.object(runner.time, "sleep"))
        # Never the real attach canary: it would start a Hermes agent.
        res = runner.run_module4(s, budget_check=lambda: budget_guard.decide(None, 1.0),
                                 attach_check=lambda *a: agent_driver.AttachCheck(True, "test"), **kw)
    return res, spawned


def test_runner():
    print("\n[4] runner: park, charge nothing, skip next time")
    wall = agent_driver.ApplyOutcome("captcha_wall", f"hCaptcha challenge on the email/sign-in step at {ORACLE}")
    s = _session()
    j = _job(s, 1878, f"https://{ORACLE}/hcmUI/CandidateExperience/en/sites/CX_1/job/1")
    res, spawned = _run(s, wall)
    s.refresh(j)
    check("agent ran once", spawned == [1878], str(spawned))
    check("no attempt charged", (j.module4_attempts or 0) == 0, str(j.module4_attempts))
    check("still should_apply (parked, not dropped)", j.should_apply is True)
    check("parked on the wall host", (j.module4_last_status or "").startswith(f"captcha_wall:{ORACLE} |"),
          j.module4_last_status)
    check("counted as captcha_parked, not failed", res.captcha_parked == 1 and res.failed == 0 and res.dropped == 0)

    res, spawned = _run(s, wall)
    check("next run: parked job not queued, no agent", spawned == [])
    check("query skips it", [r.id for r in query.list_jobs_to_apply(s)] == [])
    check("--job-id still reaches it (for testing a fix)",
          [r.id for r in query.list_jobs_to_apply(s, job_id=1878)] == [1878])

    print("\n[5] runner: another job on a walled host is parked before any agent")
    j2 = _job(s, 1879, f"https://{ORACLE}/hcmUI/CandidateExperience/en/sites/CX_1/job/2")
    j3 = _job(s, 1880, "https://www.linkedin.com/jobs/view/1880/")
    ok = agent_driver.ApplyOutcome("submitted", "submitted")
    res, spawned = _run(s, ok)
    s.refresh(j2)
    s.refresh(j3)
    check("no agent for the walled host; the LinkedIn job still runs", spawned == [1880], str(spawned))
    check("parked, uncharged", captcha_walls.is_parked(j2.module4_last_status) and not j2.module4_attempts,
          j2.module4_last_status)
    check("counted", res.captcha_parked == 1 and res.submitted == 1)

    print("\n[6] runner: a LinkedIn job behind an iCIMS wall parks without walling linkedin.com")
    s2 = _session()
    j4 = _job(s2, 1934, "https://www.linkedin.com/jobs/view/1934/")
    icims = agent_driver.ApplyOutcome("captcha_wall", f"hCaptcha challenge on the email/sign-in step at {ICIMS}")
    _run(s2, icims)
    s2.refresh(j4)
    check("parked on the iCIMS host", (j4.module4_last_status or "").startswith(f"captcha_wall:{ICIMS} |"))
    j5 = _job(s2, 1935, "https://www.linkedin.com/jobs/view/1935/")
    _, spawned = _run(s2, ok)
    check("other LinkedIn jobs are not parked", spawned == [1935], str(spawned))

    print("\n[7] runner: captcha_wall with no gate host is an ordinary captcha drop")
    s3 = _session()
    j6 = _job(s3, 1936, f"https://{OTHER}/example/jobs/1")
    _run(s3, agent_driver.ApplyOutcome("captcha_wall", f"hCaptcha challenge on {OTHER}"))
    s3.refresh(j6)
    check("charged and dropped, not parked",
          j6.module4_attempts == 1 and j6.should_apply is False and not captcha_walls.is_parked(j6.module4_last_status),
          f"{j6.module4_attempts} {j6.should_apply} {j6.module4_last_status}")


def test_site_wall_guard():
    print("\n[8] GHO-73: Paycom and Lever end the run on arrival (their challenge never shows in a snapshot)")
    g = _load("jobo_hcaptcha_site_guard_test", PLUGIN_DIR / "captcha_guard.py")
    g.reset_state()
    paycom = _fixture("paycom_guest_dialog")
    check("2197's Paycom page: the snapshot holds no hCaptcha line at all",
          "captcha" not in paycom["snapshot"].lower() and g.classify(paycom) is None)
    seen = g.observe("browser_snapshot", json.dumps(paycom))
    check("Paycom: host from the frame tree, ends as captcha_wall",
          bool(seen) and seen[0] == "hcaptcha_site" and "STATUS: captcha_wall" in seen[1]
          and f"hCaptcha wall at {PAYCOM}" in seen[1], seen and seen[1])
    check("the notice leaves a closed posting to be reported as closed", bool(seen) and "STATUS: closed" in seen[1])

    g.reset_state()
    lever = _fixture("lever_apply_enclave")
    check("2134's Lever form: only the enclave iframe, no visible challenge", g.classify(lever) is None)
    seen = g.observe("browser_navigate", json.dumps(lever))
    check("Lever: ends as captcha_wall before any field is filled",
          bool(seen) and "STATUS: captcha_wall" in seen[1] and f"hCaptcha wall at {LEVER}" in seen[1], seen and seen[1])
    eu = dict(lever, url="https://jobs.eu.lever.co/contoso/00000000-0000-4000-8000-000000000002/apply")
    seen = g.observe("browser_navigate", json.dumps(eu))
    check("EU Lever too", bool(seen) and "hCaptcha wall at jobs.eu.lever.co" in seen[1], seen and seen[1])
    v = agent_driver.parse_verdict(
        "STATUS: captcha_wall\nDETAIL: hCaptcha wall at jobs.eu.lever.co\nFIELDS_FILLED_JSON: {}")
    check("the verdict the notice asks for is a park, not a drop",
          v.status == "captcha_wall" and blockers.permanent_reason(v.status, v.detail) is None, v.status)

    g.reset_state()
    listing = {"url": "https://www.linkedin.com/jobs/view/2134/", "snapshot": '- button "Apply" [ref=e1]'}
    check("a LinkedIn listing is no wall", g.observe("browser_navigate", json.dumps(listing)) is None)
    other = dict(lever, url=f"https://{OTHER}/contoso/jobs/1")
    check("the same form on a non-wall host is no wall", g.observe("browser_navigate", json.dumps(other)) is None)
    check("Lever hosts only, not look-alikes",
          not g.is_site_wall_host("cleverco.example.com") and not captcha_walls.is_site_wall_host("notlever.co.example"))

    plugin = _load("jobo_hcaptcha_site_plugin_test", PLUGIN_DIR / "__init__.py")
    plugin.captcha_guard.reset_state()
    out = json.loads(plugin._transform_tool_result("browser_navigate", {}, json.dumps(lever)))
    check("notice survives the snapshot filter",
          str(out.get("snapshot", "")).startswith(f"[jobo: {LEVER} puts an hCaptcha"), str(out)[:200])


def test_site_wall_runner():
    print("\n[9] GHO-73: a job_url on Paycom or Lever is parked before any agent, uncharged")
    s = _session()
    j_lever = _job(s, 2134, f"https://{LEVER}/contoso/00000000-0000-4000-8000-000000000001/apply?lever-source=jobfound.org")
    j_paycom = _job(s, 2197, f"https://{PAYCOM}/v4/ats/web.php/portal/0000000000000000000000000000C0DE/jobs/100001")
    j_li = _job(s, 2198, "https://www.linkedin.com/jobs/view/2198/")
    ok = agent_driver.ApplyOutcome("submitted", "submitted")
    res, spawned = _run(s, ok)
    for j in (j_lever, j_paycom, j_li):
        s.refresh(j)
    check("no agent for the Lever and Paycom jobs; the LinkedIn job runs", spawned == [2198], str(spawned))
    check("Lever parked on its host, attempts untouched",
          (j_lever.module4_last_status or "").startswith(f"captcha_wall:{LEVER} |")
          and not j_lever.module4_attempts and j_lever.should_apply is True, j_lever.module4_last_status)
    check("Paycom parked on its host",
          (j_paycom.module4_last_status or "").startswith(f"captcha_wall:{PAYCOM} |"), j_paycom.module4_last_status)
    check("detail says why (every visitor), not 'another job on this host'",
          "every visitor" in (j_lever.module4_last_status or ""), j_lever.module4_last_status)
    check("counted as captcha_parked", res.captcha_parked == 2 and res.failed == 0 and res.dropped == 0, str(res.captcha_parked))
    check("run-report files them under captcha:wall_parked (needs_g)",
          run_report.signature_for(j_lever.module4_last_status) == "captcha:wall_parked")

    print("\n[10] GHO-73: a LinkedIn job that lands on Lever mid-run is parked, LinkedIn not walled")
    s2 = _session()
    j = _job(s2, 2201, "https://www.linkedin.com/jobs/view/2201/")
    _run(s2, agent_driver.ApplyOutcome("captcha_wall", "hCaptcha wall at jobs.lever.co"))
    s2.refresh(j)
    check("parked on jobs.lever.co, not charged",
          (j.module4_last_status or "").startswith(f"captcha_wall:{LEVER} |") and not j.module4_attempts,
          f"{j.module4_last_status} {j.module4_attempts}")
    j2 = _job(s2, 2202, "https://www.linkedin.com/jobs/view/2202/")
    _, spawned = _run(s2, ok)
    check("the next LinkedIn job still gets an agent", spawned == [2202], str(spawned))


def test_router_park():
    print("\n[11] GHO-73: the parallel router parks a listing whose Apply link is on Lever; no form agent")
    from module4 import apply_queue, linkedin_router, parallel

    tmp = Path(tempfile.mkdtemp(prefix="gho73_"))
    engine = create_engine(f"sqlite:///{tmp}/jobs.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    j = _job(s, 2300, "https://www.linkedin.com/jobs/view/2300/")
    q = apply_queue.ApplyQueue("test_gho73", path=tmp / "queue.sqlite")
    q.create_run([(2300, "Contoso")])
    li = parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=1, poll_sec=0.01)
    q.register_worker("f1", parallel.ROLE_FORM, None, None)  # a live form worker, so the LinkedIn worker routes
    route = linkedin_router.Route(linkedin_router.EXTERNAL, f"https://{LEVER}/contoso/abc/apply", "ext")

    def resolve(cdp, url):
        q.worker_done("f1")  # then the LinkedIn worker stops once nothing is left (a hand-off it would apply itself)
        return route

    with patch.object(linkedin_router, "resolve", side_effect=resolve):
        res, spawned = _run(s, agent_driver.ApplyOutcome("submitted", "submitted"), worker=li)
    s.refresh(j)
    check("no agent ran", spawned == [], str(spawned))
    check("parked on the Lever host, uncharged",
          (j.module4_last_status or "").startswith(f"captcha_wall:{LEVER} |") and not j.module4_attempts,
          j.module4_last_status)
    check("not handed to a form worker", q.pending(apply_queue.ROUTE_FORM) == 0 and res.routed_form == 0)
    check("counted as captcha_parked", res.captcha_parked == 1, str(res.captcha_parked))


def test_cutshort_turnstile():
    print("\n[12] GHO-81: Cutshort's Turnstile is a site wall: guard, router and verdict park it")
    from module4 import apply_queue, linkedin_router, parallel

    job_page = f"https://{CUTSHORT}/job/Software-Engineer-Backend-Remote-Contoso-AbC123?utm_source=linkedin-feed"
    g = _load("jobo_turnstile_site_guard_test", PLUGIN_DIR / "captcha_guard.py")
    g.reset_state()
    page = {"url": job_page, "snapshot": '- heading "Software Engineer (Backend)" [level=1]\n- button "Apply" [ref=e3]'}
    seen = g.observe("browser_navigate", json.dumps(page))
    check("a Cutshort job page ends the run as captcha_wall before sign-in or upload",
          bool(seen) and "STATUS: captcha_wall" in seen[1]
          and f"DETAIL: Cloudflare Turnstile wall at {CUTSHORT}" in seen[1], seen and seen[1])
    check("the notice names Turnstile, not hCaptcha", bool(seen) and "hCaptcha" not in seen[1], seen and seen[1])
    check("closed postings still end as closed", bool(seen) and "STATUS: closed" in seen[1])
    g.reset_state()
    seen = g.observe("browser_navigate", json.dumps(dict(page, url=f"https://www.{CUTSHORT}/profile/create-signal")))
    check("www. and the profile pages too", bool(seen) and "STATUS: captcha_wall" in seen[1], seen and seen[1])
    check("look-alikes are no wall", not captcha_walls.is_site_wall_host("notcutshort.io.example"))
    check("Paycom and Lever notices are unchanged", "hCaptcha wall at jobs.lever.co" in g.site_wall_notice(LEVER))

    v = agent_driver.parse_verdict(
        f"STATUS: captcha_wall\nDETAIL: Cloudflare Turnstile wall at {CUTSHORT}\nFIELDS_FILLED_JSON: {{}}")
    check("the verdict the notice asks for is a park", v.status == "captcha_wall"
          and blockers.permanent_reason(v.status, v.detail) is None, v.status)
    s = _session()
    j = _job(s, 1858, "https://www.linkedin.com/jobs/view/1858/")
    _, spawned = _run(s, v)
    s.refresh(j)
    check("an agent that lands on Cutshort parks the job, no attempt charged",
          spawned == [1858] and (j.module4_last_status or "").startswith(f"captcha_wall:{CUTSHORT} |")
          and not j.module4_attempts and j.should_apply is True, f"{spawned} {j.module4_last_status} {j.module4_attempts}")
    check("run-report files it under captcha:wall_parked, not captcha:turnstile",
          run_report.signature_for(j.module4_last_status) == "captcha:wall_parked", j.module4_last_status)

    tmp = Path(tempfile.mkdtemp(prefix="gho81_"))
    engine = create_engine(f"sqlite:///{tmp}/jobs.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s2 = sessionmaker(bind=engine)()
    j2 = _job(s2, 1711, "https://www.linkedin.com/jobs/view/1711/")
    q = apply_queue.ApplyQueue("test_gho81", path=tmp / "queue.sqlite")
    q.create_run([(1711, "Contoso")])
    li = parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=1, poll_sec=0.01)
    q.register_worker("f1", parallel.ROLE_FORM, None, None)
    route = linkedin_router.Route(linkedin_router.EXTERNAL, job_page, "ext")

    def resolve(cdp, url):
        q.worker_done("f1")
        return route

    with patch.object(linkedin_router, "resolve", side_effect=resolve):
        res, spawned = _run(s2, agent_driver.ApplyOutcome("submitted", "submitted"), worker=li)
    s2.refresh(j2)
    check("router: a LinkedIn listing applying on Cutshort is parked, no agent", spawned == [], str(spawned))
    check("router: parked on cutshort.io with the Turnstile detail, uncharged",
          (j2.module4_last_status or "").startswith(f"captcha_wall:{CUTSHORT} |")
          and "Turnstile" in j2.module4_last_status and not j2.module4_attempts, j2.module4_last_status)
    check("router: not handed to a form worker",
          q.pending(apply_queue.ROUTE_FORM) == 0 and res.routed_form == 0 and res.captcha_parked == 1)
    check("router: the run detail names Turnstile",
          any("Cloudflare Turnstile wall on cutshort.io" in d for d in res.details), str(res.details))


def main() -> int:
    test_guard()
    test_verdict_and_rules()
    test_runner()
    test_site_wall_guard()
    test_site_wall_runner()
    test_router_park()
    test_cutshort_turnstile()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
