#!/usr/bin/env python
"""Apply-stage token and waste controls added on 2026-09-28.

Covers:
  * hermes_plugin/jobo-browser: snapshots cut to form controls/status text
  * module4/blockers.py: failures a retry cannot fix
  * policy/paused_sites.py: temporarily paused ATS sites
  * agent_driver: ONE step budget across all handoff rounds, jobo profile,
    --ignore-rules
  * runner: drop not-retryable jobs, park paused-site jobs, stop the stage on
    a model usage limit or 3 infrastructure failures in a row

Offline: temporary SQLite DB, no browser, no model calls, no subprocesses.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import agent_driver, blockers, budget_guard, preflight, query, runner  # noqa: E402
from policy import paused_sites  # noqa: E402

PLUGIN_DIR = REPO / "hermes_plugin" / "jobo-browser"
FIXTURE = REPO / "scripts" / "fixtures" / "greenhouse_compact_snapshot.txt"

# Under the isolated runner TMPDIR points inside the suite directory.
FAKE_PDF = Path(tempfile.gettempdir()) / "jobo_test_resume.pdf"

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


sf = _load("jobo_snapshot_filter_test", PLUGIN_DIR / "snapshot_filter.py")
tk = _load("jobo_tab_keeper_test", PLUGIN_DIR / "tab_keeper.py")


# --------------------------------------------------------------------------
# snapshot filter
# --------------------------------------------------------------------------


def _refs(text: str) -> set[str]:
    import re

    return set(re.findall(r"ref=(e\d+)", text))


def test_snapshot_filter():
    print("\n[1] snapshot filter: form view of a real Greenhouse page")
    full = FIXTURE.read_text(encoding="utf-8")
    view = sf.filter_snapshot(full)
    check("under 20% of the compact tree", len(view) < len(full) * 0.2, f"{len(view)} vs {len(full)}")
    check("every actionable ref kept", _refs(view) == _refs(full), str(_refs(full) - _refs(view)))
    check("starts with the marker telling the agent about full=true", view.startswith(sf.MARKER))
    check("field labels kept", 'textbox "First Name" [required' in view)
    check("full question text kept",
          "require visa sponsorship to continue working" in view)
    check("required-field notice kept", "indicates a required field" in view)
    check("submit button kept", 'button "Submit application"' in view)
    check("job-description prose dropped", "Build Relationships" not in view)
    check("flat list (no indentation)", all(not ln.startswith(" ") for ln in view.splitlines()))

    status = "\n".join([
        "- main",
        '  - StaticText "This field is required"',
        '  - StaticText "Thank you for applying! Your application has been submitted."',
        '  - StaticText "' + ("We require candidates to verify everything they say. " * 5) + '"',
        '  - alert "Please enter a valid email"',
        '  - StaticText "Step 2 of 5"',
    ])
    v = sf.filter_snapshot(status)
    check("short error text kept", "This field is required" in v)
    check("confirmation text kept", "Thank you for applying" in v)
    check("alert kept", 'alert "Please enter a valid email"' in v)
    check("progress text kept", "Step 2 of 5" in v)
    check("long prose with a keyword dropped", "verify everything" not in v)

    opts = "- listbox \"Country\" [ref=e1]\n" + "\n".join(
        f'  - option "Country {i}" [ref=e{i + 10}]' for i in range(30)
    ) + '\n- button "Next" [ref=e99]'
    v = sf.filter_snapshot(opts)
    check("long option list cut", v.count('option "Country') == sf.MAX_OPTIONS)
    check("cut is announced", f"{30 - sf.MAX_OPTIONS} more options" in v)
    check("elements after the list still kept", 'button "Next"' in v)


def test_transform_result():
    print("\n[2] plugin hook: which results are rewritten")
    full = FIXTURE.read_text(encoding="utf-8")
    nav = json.dumps({"success": True, "url": "https://x", "title": "T", "snapshot": full,
                      "element_count": 48, "stealth_features": ["cdp_override"]})
    out = sf.transform_result("browser_navigate", {"url": "https://x"}, nav)
    data = json.loads(out) if out else {}
    check("navigate snapshot filtered", out is not None and data["snapshot"].startswith(sf.MARKER))
    check("other navigate fields untouched",
          data.get("url") == "https://x" and data.get("element_count") == 48
          and data.get("stealth_features") == ["cdp_override"])
    snap = json.dumps({"success": True, "snapshot": full, "element_count": 48})
    check("snapshot (default) filtered", sf.transform_result("browser_snapshot", {}, snap) is not None)
    check("snapshot full=true untouched",
          sf.transform_result("browser_snapshot", {"full": True}, snap) is None)
    small = json.dumps({"success": True, "snapshot": "- button \"OK\" [ref=e1]"})
    check("small snapshot untouched", sf.transform_result("browser_snapshot", {}, small) is None)
    check("other tools untouched", sf.transform_result("browser_click", {}, snap) is None)
    check("non-JSON untouched", sf.transform_result("browser_snapshot", {}, "not json snapshot") is None)
    check("error result untouched",
          sf.transform_result("browser_snapshot", {}, '{"success": false, "error": "x"}') is None)

    plugin = _load("jobo_browser_plugin_test", PLUGIN_DIR / "__init__.py")
    hooks = {}
    plugin.register(SimpleNamespace(register_hook=lambda name, cb: hooks.setdefault(name, cb)))
    check("plugin registers transform_tool_result", "transform_tool_result" in hooks)
    hooked = hooks["transform_tool_result"](tool_name="browser_snapshot", args={}, result=snap,
                                            session_id="s", duration_ms=5)
    check("hook accepts Hermes's keyword arguments", isinstance(hooked, str))
    check("hook fails open on garbage", hooks["transform_tool_result"](
        tool_name="browser_snapshot", args=None, result=object()) is None)


def test_tab_keeper():
    print("\n[2b] tab keeper: close the listing only once the agent has left it")
    listing = "https://www.linkedin.com/jobs/view/4000000106/"
    tabs = [
        {"id": "L", "url": "https://www.linkedin.com/jobs/view/4000000106/?trk=public_jobs"},
        {"id": "A", "url": "https://boards.greenhouse.io/acme/jobs/1"},
    ]
    check("same page ignores tracking params", tk.same_page(tabs[0]["url"], listing))
    check("different path is a different page",
          not tk.same_page("https://www.linkedin.com/jobs/view/1/", listing))
    check("listing closed once the agent is on the ATS",
          tk.tabs_to_close(tabs, "https://boards.greenhouse.io/acme/jobs/1", listing) == ["L"])
    check("nothing closed while the agent is still on the listing (Easy Apply)",
          tk.tabs_to_close(tabs, tabs[0]["url"], listing) == [])
    check("the only page is never closed",
          tk.tabs_to_close(tabs[:1], "https://boards.greenhouse.io/acme/jobs/1", listing) == [])
    check("no listing URL, nothing closed", tk.tabs_to_close(tabs, tabs[1]["url"], "") == [])
    check("unknown agent location, nothing closed", tk.tabs_to_close(tabs, "", listing) == [])
    check("only clicks and key presses trigger it",
          tk.TRIGGER_TOOLS == {"browser_click", "browser_press"})
    with patch.dict(os.environ, {"JOBO_SOURCE_URL": "", "BROWSER_CDP_URL": ""}):
        check("inert without a listing URL", tk.after_tool("browser_click", "t") is None)

    plugin = _load("jobo_browser_plugin_tabs_test", PLUGIN_DIR / "__init__.py")
    hooks = {}
    plugin.register(SimpleNamespace(register_hook=lambda name, cb: hooks.setdefault(name, cb)))
    check("plugin registers post_tool_call", "post_tool_call" in hooks)
    with patch.object(plugin.tab_keeper, "after_tool", side_effect=RuntimeError("boom")):
        check("post hook fails open", hooks["post_tool_call"](tool_name="browser_click", task_id="t") is None)


# --------------------------------------------------------------------------
# blockers
# --------------------------------------------------------------------------

# Verbatim DETAIL strings from real runs (data/jobs.db module4_last_status).
PERMANENT = [
    ("captcha_failed", "Lever form fully completed and resume attached, but submit is gated by an hCaptcha image-grid challenge"),
    ("stuck", "Blueyon application step 2 mandates PAN (Indian tax ID) and Date of Birth, neither of which is in the candidate facts; cannot fabricate tax/personal ID data"),
    ("stuck", "Brightloop RippleHire application form has hard-required Date of Birth (day/month/year) and Current Company fields with no available/truthful data"),
    ("stuck", "jobs.apple.com requires Apple ID sign-in at idmsa.apple.com; existing Apple ID for alex.example@example.com only supports trusted-device/SMS-2FA recovery"),
    ("stuck", "Microsoft's Eightfold careers site only offers OAuth sign-up (Microsoft, LinkedIn, Google, Facebook), no email/password option"),
    ("stuck", "GREENMOOR's SuccessFactors careers portal requires an account; account already exists for alex.example@example.com from a prior attempt, but Send Password Reset Email produced no email"),
    ("stuck", "Fabrikam portal requires signup gated by an email OTP wall that has already been confirmed non-functional/undeliverable on a prior retry"),
    ("stuck", "Myrtleby's application form requires mandatory 10th/12th/Graduation percentage+year fields not present in candidate facts or resume"),
    ("stuck", "Company career-page form's Cloudflare Turnstile widget errors out (600010) and never issues a token"),
    # GHO-103: photo-required forms (G skips them, GHO-60), however worded.
    ("stuck", "Tailspin's Workable form requires a photo image upload (input required=true, accept jpg/png/gif) which is not among the provided candidate files; resume already attached and all other fields filled."),
    ("stuck", "Litware create-profile form requires a mandatory \"Candidate Photo\" upload (passport-style photo) to proceed past Personal Information; no photo asset exists for the candidate and none may be fabricated."),
]
# GHO-103: a photo the form merely offers, or a photo of something else, is
# no reason to give up.
PHOTO_REQUIRED_TOO = (
    "Litware registration form requires a mandatory Candidate Photo (passport-style) upload with "
    "input[type=file] required=true; no photo asset was provided for this candidate"
)
PHOTO_NOT_REQUIRED = [
    "Optional profile picture upload skipped; Submit button stays disabled after the resume upload",
    "Resume upload field rejects the PDF; only image upload formats are listed, form not submitted",
]
# GHO-103: verbatim from job 2886 (run 2026-10-30 10:00), host pseudonymised.
NO_WAY_TO_APPLY = ("Northwind.example/careers/python-developer is a Google Sites page with only a job description; "
                   "no apply form, button, or email address found on the page")
APPLY_PATH_NOT_SEEN = [
    # The page did not show its form: a retry (or a fix) can still get there.
    "No apply form, button or email visible because the careers iframe did not load",
    "No apply button found; the Apply link opens an external ATS that needs sign-in",
    "no apply form or button found on the listing",
]
# Permanent only while email apply is off (GHO-67); otherwise the retry lets
# the agent report it as `email_apply`.
NO_WEB_FORM = ("stuck", "Apply flow has no web form; \"Apply Now\" is a mailto:careers@quillonpub.com link only")
RETRYABLE = [
    ("stuck", "Reached max tool-call iterations while on the \"Application Questions\" step (step 3 of 5) of Plover's Workday application"),
    ("stuck", "ran out of step budget (80 model steps) mid-form, before submitting"),
    ("stuck", "Keka ATS resume upload (id=\"resume-upload\", duplicated with another class=\"resume-upload\" input) fails NEED_UPLOAD with ambiguous file-input slots"),
    ("stuck", "Greenhouse react-select fields (Country, School, Degree) at Bluegum's application form silently reject programmatic value-setting"),
    ("stuck", "Merlin Workday form filled through My Information (resume uploaded, all fields/consents set) but the Next button won't advance"),
    ("stuck", "agent produced no verdict; last output: API call failed after 3 retries: Hermes can't reach the model provider."),
    ("stuck", "Form fully filled; the required \"Date available\" field rejected the typed value until re-entered"),
    # Parked, not dropped, since 2026-10-03: one Google sign-in opens these
    # (module4/google_forms.py, scripts/test_google_forms.py).
    ("stuck", "Application redirects to a Google Form that requires Google account sign-in, and no Google credentials/session are available"),
    ("stuck", ""),
]


def test_blockers():
    print("\n[3] blockers: what is not worth another attempt")
    for status, detail in PERMANENT:
        check(f"permanent: {detail[:60]}...", blockers.permanent_reason(status, detail) is not None)
    for status, detail in RETRYABLE:
        check(f"retryable: {detail[:60] or '<empty>'}...", blockers.permanent_reason(status, detail) is None,
              f"got {blockers.permanent_reason(status, detail)!r}")
    with patch.dict(os.environ, {"JOBO_EMAIL_APPLY": "off"}):
        check("no web form: permanent with email apply off", blockers.permanent_reason(*NO_WEB_FORM) is not None)
    with patch.dict(os.environ, {"JOBO_EMAIL_APPLY": "send"}):
        check("no web form: retryable with email apply on", blockers.permanent_reason(*NO_WEB_FORM) is None,
              f"got {blockers.permanent_reason(*NO_WEB_FORM)!r}")
    for detail in [d for _, d in PERMANENT[-2:]] + [PHOTO_REQUIRED_TOO]:
        check(f"photo required: {detail[:50]}...", blockers.photo_required(detail))
    for detail in PHOTO_NOT_REQUIRED:
        check(f"photo not required: {detail[:50]}...", not blockers.photo_required(detail))
    check("no apply form, button or email -> dead listing",
          blockers.dead_listing_reason("stuck", NO_WAY_TO_APPLY) is not None)
    check("no apply form ... only from a stuck verdict", blockers.dead_listing_reason("failed", NO_WAY_TO_APPLY) is None)
    for detail in APPLY_PATH_NOT_SEEN:
        check(f"not a dead listing: {detail[:50]}...", blockers.dead_listing_reason("stuck", detail) is None,
              repr(blockers.dead_listing_reason("stuck", detail)))
    check("stored status form parsed",
          blockers.permanent_reason_from_last_status("captcha_failed:hCaptcha grid") is not None)
    check("empty stored status is retryable", blockers.permanent_reason_from_last_status(None) is None)


# --------------------------------------------------------------------------
# paused sites
# --------------------------------------------------------------------------


def test_paused_sites():
    print("\n[4] paused sites")
    check("rippling URL paused",
          paused_sites.paused_site_for_url("https://ats.rippling.com/en-GB/westbay/jobs/x/apply") == "ats.rippling.com")
    check("LinkedIn URL not paused", paused_sites.paused_site_for_url("https://www.linkedin.com/jobs/view/1/") is None)
    check("look-alike host not paused",
          paused_sites.paused_site_for_url("https://ats.rippling.com.evil.example/x") is None)
    check("site found in agent detail",
          paused_sites.paused_site_in_text("apply flow routes through ats.rippling.com, which is paused") == "ats.rippling.com")
    st = paused_sites.status_for("ats.rippling.com", "apply flow routes through ats.rippling.com")
    check("status recorded with prefix", st.startswith("paused:ats.rippling.com | "))
    check("still paused while listed", paused_sites.still_paused(st))
    check("released once unlisted", not paused_sites.still_paused("paused:old.example.com | x"))
    check("ordinary status is not paused", not paused_sites.still_paused("stuck:whatever"))
    with patch.dict(os.environ, {"JOBO_PAUSED_SITES": "Jobs.Example.com"}):
        check("env adds a site", "jobs.example.com" in paused_sites.paused_sites())
    with patch.dict(os.environ, {"JOBO_PAUSED_SITES": ""}):
        check("env cannot remove the built-in list", "ats.rippling.com" in paused_sites.paused_sites())


# --------------------------------------------------------------------------
# agent driver: step budget and command
# --------------------------------------------------------------------------


def test_step_budget_across_handoffs():
    print("\n[5] driver: one step budget for the whole job, across handoffs")
    calls = []
    counts = iter([30, 60, 85])

    def fake_run(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append({"max_turns": max_turns, "resume": resume_session})
        return "working...\nNEED_FIELD: #1 = India\n\nsession_id: s1\n", False

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", side_effect=lambda sid, p: next(counts)))
        st.enter_context(patch.object(agent_driver, "_resolve_field", return_value="FIELD_SET: #1"))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": ""}))
        out = agent_driver.run_apply_agent(
            system_prompt="S", user_prompt="U", cdp_url="http://x", job_id=1, resume_path="/r.pdf"
        )
    budget = agent_driver.AGENT_STEPS_FIRST
    check("first round gets the whole budget", calls[0]["max_turns"] == str(budget), calls[0]["max_turns"])
    check("second round gets only what is left", calls[1]["max_turns"] == str(budget - 30), calls[1]["max_turns"])
    check("third round shrinks again", calls[2]["max_turns"] == str(budget - 60), calls[2]["max_turns"])
    check("no fourth round once the budget is spent", len(calls) == 3, f"{len(calls)} rounds")
    check("resumes the same session", calls[1]["resume"] == "s1")
    check("verdict is stuck", out.status == "stuck")
    check("detail says the budget ran out", "step budget" in out.detail, out.detail)
    check("runner reads it as budget exhaustion (next try gets more room)",
          runner._is_budget_exhaustion(f"stuck:{out.detail}"))

    check("retry budget is smaller", agent_driver.step_budget(is_retry=True, budget_boost=False) < budget)
    check("escalated budget is larger", agent_driver.step_budget(is_retry=True, budget_boost=True) > budget)
    with patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": "7"}):
        check("env override wins", agent_driver.step_budget(is_retry=False, budget_boost=False) == 7)


def test_hermes_command():
    print("\n[6] driver: hermes command line")
    seen = {}

    def fake_subprocess_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env", {})
        return SimpleNamespace(stdout="STATUS: submitted\nDETAIL: ok\nsession_id: s9\n", stderr="")

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_hermes_bin", return_value="/usr/bin/hermes"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.object(agent_driver.subprocess, "run", side_effect=fake_subprocess_run))
        agent_driver._run_hermes(prompt="p", cdp_url="http://127.0.0.1:9333", timeout_sec=5, max_turns="12")
    cmd = seen["cmd"]
    check("runs in the jobo profile, before the subcommand",
          cmd[1:4] == ["-p", "jobo", "chat"], str(cmd[:5]))
    check("--ignore-rules keeps persona and memory out", "--ignore-rules" in cmd)
    check("browser + vision tools only", cmd[cmd.index("-t") + 1] == agent_driver.APPLY_AGENT_TOOLSETS)
    check("max turns passed through", cmd[cmd.index("--max-turns") + 1] == "12")
    check("CDP URL handed to the browser tools", seen["env"].get("BROWSER_CDP_URL") == "http://127.0.0.1:9333")

    with tempfile.TemporaryDirectory() as home:
        with patch.object(agent_driver.Path, "home", return_value=Path(home)):
            with patch.dict(os.environ, {"JOBO_HERMES_PROFILE": "jobo"}):
                check("missing profile falls back to default", agent_driver.hermes_profile() is None)
            (Path(home) / ".hermes" / "profiles" / "jobo").mkdir(parents=True)
            with patch.dict(os.environ, {"JOBO_HERMES_PROFILE": "jobo"}):
                check("existing profile is used", agent_driver.hermes_profile() == "jobo")
            with patch.dict(os.environ, {"JOBO_HERMES_PROFILE": "default"}):
                check("'default' means no -p", agent_driver.hermes_profile() is None)
            check("state DB path follows the profile",
                  agent_driver._state_db("jobo") == Path(home) / ".hermes" / "profiles" / "jobo" / "state.db")


# --------------------------------------------------------------------------
# runner, with a temporary DB and fake agent
# --------------------------------------------------------------------------


def make_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), tmp.name


def add_job(session, jid, *, url=None, last_status=None, attempts=0, pdf=None, fit=None):
    pdf = pdf or str(FAKE_PDF)
    session.add(JobModel(
        id=jid, company_name=f"Co{jid}", job_title="SWE",
        job_url=url or f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False, referred_json="[]", metadata_json="{}",
        module2_attempted=True, module2_enriched_at=datetime.now(),
        should_apply=True, module3_fit_score=5 - (jid % 3) if fit is None else fit,
        module3_resume_pdf_path=pdf, module3_cover_pdf_path=pdf,
        module4_attempts=attempts, module4_last_status=last_status,
    ))
    session.commit()


def run_with(session, outcomes: dict):
    """run_module4 with every external dependency faked. outcomes: job_id -> ApplyOutcome."""
    spawned = []

    def fake_agent(*, job_id, **kw):
        spawned.append(job_id)
        return outcomes[job_id]

    open_pf = preflight.PreflightResult(closed=False, reason="open", checked=True)
    with ExitStack() as st:
        st.enter_context(patch.object(runner.browser_session, "ensure_browser_running", return_value="http://cdp"))
        st.enter_context(patch.object(runner.browser_session, "ensure_healthy", return_value="http://cdp"))
        st.enter_context(patch.object(runner.browser_session, "close_extra_tabs"))
        st.enter_context(patch.object(
            runner.linkedin_session, "check_session",
            return_value=runner.linkedin_session.SessionState("signed_in", "test")))
        st.enter_context(patch.object(runner.preflight, "check_job_open", return_value=open_pf))
        st.enter_context(patch.object(runner.circuit_breaker, "is_tripped", return_value=None))
        st.enter_context(patch.object(runner.circuit_breaker, "record_failure"))
        st.enter_context(patch.object(runner.submission_audit, "write_audit_record"))
        st.enter_context(patch.object(runner.agent_driver, "run_apply_agent", side_effect=fake_agent))
        st.enter_context(patch.object(runner.constants, "apply_delay_seconds", return_value=0))
        st.enter_context(patch.object(runner.constants, "delay_jitter_seconds", return_value=0))
        st.enter_context(patch.object(runner.constants, "max_jobs_per_run", return_value=0))
        result = runner.run_module4(session, budget_check=lambda: budget_guard.decide(None, 1.0))
    return result, spawned


def test_runner_controls():
    print("\n[7] runner: not-retryable, paused sites, model-limit stop")
    FAKE_PDF.write_bytes(b"%PDF-fake")
    session, db = make_session()
    Outcome = agent_driver.ApplyOutcome
    try:
        add_job(session, 1, attempts=1,
                last_status="stuck:Blueyon application step 2 mandates PAN and Date of Birth, neither of which is in the candidate facts")
        add_job(session, 2, url="https://ats.rippling.com/en-GB/acme/jobs/1/apply")
        add_job(session, 3)
        add_job(session, 4)
        add_job(session, 5, attempts=1, last_status="stuck:ran out of step budget (80 model steps) mid-form")
        outcomes = {
            3: Outcome(status="paused_site", detail="apply flow routes through ats.rippling.com, which is paused"),
            4: Outcome(status="captcha_failed", detail="hCaptcha image grid blocks Submit"),
            5: Outcome(status="submitted", detail="confirmation page shown"),
        }
        result, spawned = run_with(session, outcomes)
        rows = {j.id: j for j in session.query(JobModel).all()}

        check("not-retryable job dropped without an agent", 1 not in spawned and rows[1].should_apply is False)
        check("paused-site URL skipped without an agent", 2 not in spawned)
        check("paused-site URL parked, not charged",
              rows[2].module4_last_status.startswith("paused:") and rows[2].module4_attempts == 0
              and rows[2].should_apply is True)
        check("agent-reported paused site parked, not charged",
              rows[3].module4_last_status.startswith("paused:ats.rippling.com")
              and rows[3].module4_attempts == 0 and rows[3].should_apply is True)
        check("captcha dropped after ONE attempt", rows[4].module4_attempts == 1 and rows[4].should_apply is False)
        check("budget-exhausted job still retried (and here submitted)", 5 in spawned and rows[5].applied is True)
        check("counters", (result.permanent_blocked, result.paused, result.submitted) == (2, 2, 1),
              str((result.permanent_blocked, result.paused, result.submitted)))
        queued = {j.id for j in query.list_jobs_to_apply(session)}
        check("paused jobs stay out of the queue", not ({2, 3} & queued), str(queued))
        check("--job-id still reaches a paused job", [j.id for j in query.list_jobs_to_apply(session, job_id=3)] == [3])
    finally:
        session.close()
        os.unlink(db)

    # GHO-76: a ZipRecruiter apply link that answers 500 on every load is a
    # listing that is gone (job 1868, 2026-10-18 and 10-21 runs): closed.
    session, db = make_session()
    zr = "https://www.ziprecruiter.com/kn/AbCdEf0123"
    zr_500 = ("ZipRecruiter apply link returns \"SOMETHING WENT WRONG / Internal Server Error\" consistently "
              "on two attempts, no job content or apply control ever loads")
    try:
        add_job(session, 20, url=zr)
        add_job(session, 21, url=zr, attempts=1, last_status=f"stuck:{zr_500}")
        add_job(session, 22, url=zr)
        outcomes = {
            20: Outcome(status="stuck", detail=zr_500),
            # One bad load and then a different failure is not a dead listing.
            22: Outcome(status="stuck", detail="ZipRecruiter showed an Internal Server Error once; after a "
                                               "reload the form loaded but the resume upload never registered"),
        }
        result, spawned = run_with(session, outcomes)
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("ZipRecruiter persistent 500 -> closed, no attempt charged",
              rows[20].module4_outcome == "closed" and rows[20].module4_closed_at is not None
              and rows[20].should_apply is False and rows[20].module4_attempts == 0,
              f"{rows[20].module4_outcome} attempts={rows[20].module4_attempts}")
        check("stored ZipRecruiter 500 -> closed without an agent",
              21 not in spawned and rows[21].module4_outcome == "closed", f"{rows[21].module4_outcome}")
        check("one ZipRecruiter 500 then another failure stays a charged failure",
              rows[22].module4_outcome != "closed" and rows[22].module4_attempts == 1,
              f"{rows[22].module4_outcome} attempts={rows[22].module4_attempts}")
        check("closed counter", result.closed == 2, str(result.closed))
    finally:
        session.close()
        os.unlink(db)

    # GHO-103: a photo-required form is a GHO-60 policy skip after ONE
    # attempt, and a page with no way to apply is closed, both now and when
    # already stored as stuck (jobs 3096 and 2886, 2026-10-30 10:00 run).
    session, db = make_session()
    photo = PERMANENT[-2][1]
    try:
        add_job(session, 30, url="https://apply.workable.com/tailspin/j/ABC123/")
        add_job(session, 31, attempts=1, last_status=f"stuck:{photo}")
        add_job(session, 32, url="https://northwind.example/careers/python-developer")
        add_job(session, 33, attempts=1, last_status=f"stuck:{NO_WAY_TO_APPLY}")
        outcomes = {
            30: Outcome(status="stuck", detail=photo),
            32: Outcome(status="stuck", detail=NO_WAY_TO_APPLY),
        }
        result, spawned = run_with(session, outcomes)
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("photo-required -> skipped by policy after one attempt",
              rows[30].module4_outcome == "skipped_by_policy" and rows[30].should_apply is False
              and rows[30].module4_attempts == 1
              and rows[30].module4_last_status.startswith(blockers.PHOTO_SKIP_STATUS)
              and "photo image upload" in rows[30].module4_last_status,
              f"{rows[30].module4_outcome} {rows[30].module4_last_status[:80]!r}")
        check("stored photo-required stuck -> skipped by policy without an agent",
              31 not in spawned and rows[31].module4_outcome == "skipped_by_policy"
              and rows[31].should_apply is False, f"{rows[31].module4_outcome}")
        check("no apply form, button or email -> closed, no attempt charged",
              rows[32].module4_outcome == "closed" and rows[32].should_apply is False
              and rows[32].module4_attempts == 0, f"{rows[32].module4_outcome} attempts={rows[32].module4_attempts}")
        check("stored no-way-to-apply stuck -> closed without an agent",
              33 not in spawned and rows[33].module4_outcome == "closed", f"{rows[33].module4_outcome}")
    finally:
        session.close()
        os.unlink(db)

    session, db = make_session()
    try:
        for jid in range(10, 16):
            add_job(session, jid)
        limit_detail = ("agent produced no verdict; last output: Claude didn't answer after 3 attempts. "
                        "Provider said: You've hit your session limit · resets 3:20am")
        outcomes = {jid: Outcome(status="stuck", detail=limit_detail) for jid in range(10, 16)}
        result, spawned = run_with(session, outcomes)
        check("usage limit stops the stage at the FIRST hit", len(spawned) == 1, f"spawned {spawned}")
        check("stop is reported", any("usage limit" in d for d in result.details))
        rows = session.query(JobModel).all()
        check("no job charged an attempt", all((j.module4_attempts or 0) == 0 for j in rows))
    finally:
        session.close()
        os.unlink(db)

    session, db = make_session()
    try:
        for jid in range(20, 27):
            add_job(session, jid)
        infra = "agent produced no verdict; last output: API call failed after 3 retries: Hermes can't reach the model provider."
        outcomes = {jid: Outcome(status="stuck", detail=infra) for jid in range(20, 27)}
        result, spawned = run_with(session, outcomes)
        check(f"{runner.MAX_CONSECUTIVE_INFRA_FAILURES} infra failures in a row stop the stage",
              len(spawned) == runner.MAX_CONSECUTIVE_INFRA_FAILURES, f"spawned {spawned}")
        check("infra stop is reported", any("in a row" in d for d in result.details))
    finally:
        session.close()
        os.unlink(db)

    session, db = make_session()
    try:
        for jid in range(30, 36):
            add_job(session, jid, fit=4)  # equal scores: the queue runs in id order
        infra = "agent produced no verdict; last output: API call failed after 3 retries: Hermes can't reach the model provider."
        ok = Outcome(status="stuck", detail="Greenhouse react-select Country rejects the value")
        seq = {30: Outcome(status="stuck", detail=infra), 31: Outcome(status="stuck", detail=infra), 32: ok,
               33: Outcome(status="stuck", detail=infra), 34: Outcome(status="stuck", detail=infra), 35: ok}
        result, spawned = run_with(session, seq)
        check("a real outcome resets the infra streak", len(spawned) == 6, f"spawned {spawned}")
    finally:
        session.close()
        os.unlink(db)


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls main()."""
    print("apply token and waste controls")
    test_snapshot_filter()
    test_transform_result()
    test_tab_keeper()
    test_blockers()
    test_paused_sites()
    test_step_budget_across_handoffs()
    test_hermes_command()
    test_runner_controls()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
