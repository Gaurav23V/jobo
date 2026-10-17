#!/usr/bin/env python
"""LinkedIn session safety for the apply stage (TODO item 10, 2026-10-03).

What happened: Humongo job 1005 ran `browser_cdp Storage.clearCookies` on the
shared apply profile (06:44:07), LinkedIn's session went with it, job 1007
then tried to sign in to LinkedIn itself (password-reset page + emailed PIN),
and every later LinkedIn job failed "LinkedIn requires sign-in" and was
charged an attempt. Wingtip job 818 typed three invented account passwords.

Covers:
  [1] linkedin_session wording / URL / sender classifiers on the real texts
  [2] cookie decision (names + expiry only)
  [3] plugin guard: cookie wipes, LinkedIn/Google sign-in, signed-out page,
      invented passwords (the exact calls from the 2026-10-03 transcripts)
  [4] driver: OTP / reset refusals for LinkedIn mail, standard password forced
  [5] blockers + reset_attempts: a sign-out is never permanent, always ours
  [6] runner: stop before the next job on a signed-out session, no attempt
      charged, DEGRADED health; agent-reported sign-in confirmed or forgiven
  [7] LIVE against the harness's own headless Chromium (never 9333): a fresh
      profile is signed out; a dummy li_at cookie set in THAT profile reads
      as signed in; cookie values never leave the module

Offline except [7], which uses only the browser the isolated runner launched
for this suite (JOBO_BROWSER_CDP_PORT). Run via
    bash scripts/run_regression_tests.sh test_linkedin_session
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
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
from module4 import (  # noqa: E402
    agent_driver,
    blockers,
    browser_session,
    budget_guard,
    form_filler,
    linkedin_session as ls,
    password_reset,
    preflight,
    runner,
)
from scripts.reset_attempts import classify  # noqa: E402

PLUGIN_DIR = REPO / "hermes_plugin" / "jobo-browser"
FAKE_PDF = Path(tempfile.gettempdir()) / "jobo_test_resume_li.pdf"

# The isolated runner overwrites this with its own port's URL.
CDP_URL = browser_session.cdp_url()

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


# Verbatim agent verdicts from the 2026-10-03 run (data/jobs.db, run logs).
REAL_SIGNED_OUT = {
    1007: 'LinkedIn login for alex.example@example.com passed the email OTP step but then requires "Check your '
          'LinkedIn app" push approval or SMS verification (no email-retrievable path), blocking access to '
          "the Apply flow that leads to Humongo's Paradox.ai chat application.",
    1140: "LinkedIn requires sign-in (Google OAuth or email/password) before the Easy Apply form loads; no "
          "LinkedIn credentials or vault entry exist for this account, and there is no password to reset "
          "since no account/vault entry exists.",
    1173: "LinkedIn job requires sign-in to view/apply, and no LinkedIn credentials are saved in the vault; "
          "cannot proceed past the login wall.",
    1220: "LinkedIn requires signing in (no guest apply) before the Wideworld apply flow loads; no "
          "LinkedIn account credentials",
}
NOT_SIGNED_OUT = [
    "Microsoft's Eightfold careers site only offers OAuth sign-up (Microsoft, LinkedIn, Google, Facebook), "
    "no email/password option, confirmed on repeat attempt per retry notice.",
    'LinkedIn posting for Oakhurst Software Engineer II shows "No longer accepting applications"',
    "LinkedIn Easy Apply modal reproducibly fails to advance past page 1/4 (Contact info)",
    "LinkedIn Easy Apply form gated by an invisible Google reCAPTCHA Enterprise widget",
    "Workday requires sign-in; the account exists and the reset email never arrived. Found via LinkedIn.",
    "Workday create-account page asks for a LinkedIn profile URL and a sign-in password",
    "Greenhouse react-select fields reject the value",
    "",
]


def test_classifiers():
    print("\n[1] classifiers: wording, URLs, senders")
    for jid, text in REAL_SIGNED_OUT.items():
        check(f"job {jid} verdict reads as a sign-out", ls.is_signed_out_detail(text))
    for text in NOT_SIGNED_OUT:
        check(f"not a sign-out: {text[:55] or '<empty>'}", not ls.is_signed_out_detail(text))
    check("explicit status counts", ls.is_signed_out_outcome("linkedin_signed_out", ""))
    check("stored status form counts", ls.is_signed_out_detail("linkedin_signed_out:anything"))

    auth = [
        "https://www.linkedin.com/login?fromSignIn=true",
        "https://www.linkedin.com/authwall?trk=gf",
        "https://www.linkedin.com/checkpoint/challenge/AQHiYA9t/?ut=344an",
        "https://www.linkedin.com/uas/login-submit",
        "https://www.linkedin.com/uas/request-password-reset?trk=csm-v2_forgot_password",
    ]
    for u in auth:
        check(f"auth URL: {u[:60]}", ls.is_linkedin_auth_url(u))
    for u in ("https://www.linkedin.com/feed/", "https://www.linkedin.com/jobs/view/4000000104/",
              "https://evil.example/login", "https://linkedin.com.evil.example/login"):
        check(f"not auth URL: {u}", not ls.is_linkedin_auth_url(u))

    for s in ("linkedin.com", "LinkedIn <security-noreply@linkedin.com>", "accounts.google.com", "google.com"):
        check(f"protected sender: {s}", ls.is_protected_account_domain(s))
    for s in ("myworkday.com", "northwind.com", "unknown", "humongo.paradox.ai", ""):
        check(f"ordinary sender: {s or '<empty>'}", not ls.is_protected_account_domain(s))


def test_cookie_decision():
    print("\n[2] cookie decision uses names/expiry only")
    now = 1_800_000_000.0
    check("no cookies -> signed out", ls.session_from_cookies([], now=now).signed_out)
    guest = [{"name": "bcookie", "domain": ".linkedin.com", "expires": now + 9e6},
             {"name": "JSESSIONID", "domain": ".www.linkedin.com", "expires": now + 9e6}]
    check("guest cookies only -> signed out", ls.session_from_cookies(guest, now=now).signed_out)
    live = guest + [{"name": "li_at", "domain": ".www.linkedin.com", "expires": now + 9e6}]
    check("li_at present -> signed in", ls.session_from_cookies(live, now=now).signed_in)
    old = guest + [{"name": "li_at", "domain": ".www.linkedin.com", "expires": now - 5}]
    check("expired li_at -> signed out", ls.session_from_cookies(old, now=now).signed_out)
    sess = [{"name": "li_at", "domain": ".linkedin.com", "expires": -1, "session": True}]
    check("session-only li_at -> signed in", ls.session_from_cookies(sess, now=now).signed_in)

    # Cookie VALUES must be dropped at the boundary.
    fake = {"cookies": [{"name": "li_at", "value": "SECRET-VALUE", "domain": ".www.linkedin.com",
                         "expires": now + 9e6},
                        {"name": "sid", "value": "OTHER", "domain": ".google.com", "expires": now + 9e6}]}
    with patch.object(ls, "_browser_ws", return_value="ws://x"), \
         patch.object(ls, "_cdp_call", return_value=fake):
        names = ls.linkedin_cookie_names("http://x")
    check("only .linkedin.com cookies kept", [c["name"] for c in names] == ["li_at"], str(names))
    check("no cookie value leaves the module", "SECRET-VALUE" not in json.dumps(names))

    with patch.object(ls, "linkedin_cookie_names", side_effect=OSError("refused")):
        check("browser down -> unknown, never signed out", ls.check_session("http://x").state == "unknown")
    with patch.object(ls, "linkedin_cookie_names", return_value=live), \
         patch.object(ls, "_page_urls", return_value=["https://www.linkedin.com/checkpoint/challenge/x"]):
        check("tab parked on a checkpoint -> signed out", ls.check_session("http://x").signed_out)
    with patch.object(ls, "linkedin_cookie_names", return_value=live), \
         patch.object(ls, "_page_urls", return_value=["https://www.linkedin.com/jobs/view/1/"]):
        check("cookie + normal tab -> signed in", ls.check_session("http://x").signed_in)


def test_plugin_guard():
    print("\n[3] plugin guard (pre_tool_call)")
    g = _load("jobo_account_guard_test", PLUGIN_DIR / "account_guard.py")
    g.reset_state()
    check("guard password equals the pipeline's", g.STANDARD_PASSWORD == password_reset.STANDARD_PASSWORD)

    # Exact call job 1005 made at 06:44:07.
    wipe = {"method": "Storage.clearCookies", "params": {"browserContextId": "214ADA4D1055E9D76B9C871F67AC2C93"}}
    check("job 1005's Storage.clearCookies is blocked", bool(g.check_tool_call("browser_cdp", wipe)))
    check("Network.clearBrowserCookies blocked",
          bool(g.check_tool_call("browser_cdp", {"method": "Network.clearBrowserCookies"})))
    check("clearDataForOrigin on linkedin.com blocked", bool(g.check_tool_call(
        "browser_cdp", {"method": "Storage.clearDataForOrigin",
                        "params": {"origin": "https://www.linkedin.com", "storageTypes": "all"}})))
    check("clearDataForOrigin with no origin blocked", bool(g.check_tool_call(
        "browser_cdp", {"method": "Storage.clearDataForOrigin", "params": {}})))
    check("clearDataForOrigin on the ATS itself allowed", g.check_tool_call(
        "browser_cdp", {"method": "Storage.clearDataForOrigin",
                        "params": {"origin": "https://humongo.paradox.ai", "storageTypes": "all"}}) is None)
    check("deleteCookies for .linkedin.com blocked", bool(g.check_tool_call(
        "browser_cdp", {"method": "Network.deleteCookies", "params": {"name": "li_at", "domain": ".linkedin.com"}})))
    check("Browser.close blocked", bool(g.check_tool_call("browser_cdp", {"method": "Browser.close"})))
    check("ordinary CDP allowed", g.check_tool_call("browser_cdp", {"method": "Target.getTargets", "params": {}}) is None)
    check("console cookie wipe blocked", bool(g.check_tool_call(
        "browser_console", {"expression": "document.cookie='li_at=; expires=Thu, 01 Jan 1970'"})))
    check("console localStorage.clear blocked", bool(g.check_tool_call(
        "browser_console", {"expression": "localStorage.clear(); location.reload()"})))
    check("ordinary console allowed", g.check_tool_call(
        "browser_console", {"expression": "document.body.innerText.slice(0,500)"}) is None)

    check("navigate to LinkedIn login blocked", bool(g.check_tool_call(
        "browser_navigate", {"url": "https://www.linkedin.com/login"})))
    check("navigate to LinkedIn password reset blocked", bool(g.check_tool_call(
        "browser_navigate", {"url": "https://www.linkedin.com/uas/request-password-reset"})))
    check("navigate to Google sign-in blocked", bool(g.check_tool_call(
        "browser_navigate", {"url": "https://accounts.google.com/signin/v2"})))
    check("navigate to a job listing allowed", g.check_tool_call(
        "browser_navigate", {"url": "https://www.linkedin.com/jobs/view/4000000104/"}) is None)

    # 1007's first navigate result: LinkedIn guest view.
    guest = json.dumps({"success": True, "url": "https://www.linkedin.com/jobs/view/4000000104/",
                        "snapshot": '- generic\n  - dialog "Sign in to see who you already know at Vanarsdel"\n'
                                    '    - button "Dismiss" [ref=e1]\n'})
    out = g.annotate("browser_navigate", guest)
    check("guest page gets a signed-out notice", bool(out) and "linkedin_signed_out" in out)
    msg = g.check_tool_call("browser_click", {"ref": "@e1"})
    check("dismissing the sign-in modal is refused", bool(msg) and "STATUS: linkedin_signed_out" in msg)
    check("typing on the signed-out page is refused",
          bool(g.check_tool_call("browser_type", {"ref": "@e12", "text": "alex.example@example.com"})))
    later = json.dumps({"success": True, "url": "https://www.linkedin.com/jobs/view/4000000104/",
                        "snapshot": '- heading "Full Stack Developer I" [level=1, ref=e13]'})
    g.annotate("browser_snapshot", later)
    check("flag sticks while still on LinkedIn", bool(g.check_tool_call("browser_click", {"ref": "@e13"})))
    ats = json.dumps({"success": True, "url": "https://humongo.paradox.ai/co/x", "snapshot": "- textbox [ref=e2]"})
    g.annotate("browser_navigate", ats)
    check("flag lifts on the employer's site", g.check_tool_call("browser_click", {"ref": "@e2"}) is None)

    signed_in = json.dumps({"success": True, "url": "https://www.linkedin.com/jobs/view/1/",
                            "snapshot": '- button "Easy Apply to Acme" [ref=e5]\n- link "Messaging" [ref=e6]'})
    g.reset_state()
    check("signed-in LinkedIn page is not flagged", g.annotate("browser_navigate", signed_in) is None)
    check("signed-in page clicks allowed", g.check_tool_call("browser_click", {"ref": "@e5"}) is None)

    # Wingtip 818: invented passwords.
    g.reset_state()
    form = json.dumps({"success": True, "url": "https://wingtip.avature.net/register",
                       "snapshot": '- textbox "Email *" [ref=e3]\n- textbox "Password *" [ref=e59]\n'
                                   '- textbox "Confirm Password *" [ref=e60]\n- textbox "Skills" [ref=e61]'})
    g.annotate("browser_snapshot", form)
    for bad in ("Alex@Zeno2026!", "Alex#Zeno2026Secure!", "AlexV2026!Secure"):
        check(f"password field: {bad} rewritten to the standard one",
              g.password_rewrite("browser_type", {"ref": "@e59", "text": bad}) == {"text": g.STANDARD_PASSWORD}
              and g.check_tool_call("browser_type", {"ref": "@e59", "text": bad}) is None)
        check(f"unknown field: {bad} refused",
              bool(g.check_tool_call("browser_type", {"ref": "@e99", "text": bad})))
    check("standard password untouched",
          g.password_rewrite("browser_type", {"ref": "@e60", "text": g.STANDARD_PASSWORD}) is None)
    for ok in ("alex.example@example.com", "+1 555 0100", "https://github.com/example", "Python3.11",
               "24 LPA base", "Springfield City", "Node.js-18"):
        check(f"ordinary value allowed: {ok}", g.check_tool_call("browser_type", {"ref": "@e61", "text": ok}) is None
              and g.password_rewrite("browser_type", {"ref": "@e61", "text": ok}) is None)

    plugin = _load("jobo_browser_plugin_li_test", PLUGIN_DIR / "__init__.py")
    hooks = {}
    plugin.register(SimpleNamespace(register_hook=lambda name, cb: hooks.setdefault(name, cb)))
    check("plugin registers pre_tool_call", "pre_tool_call" in hooks)
    blocked = hooks["pre_tool_call"](tool_name="browser_cdp", args=wipe, task_id="t", session_id="s")
    check("hook returns a block directive", isinstance(blocked, dict) and blocked.get("action") == "block"
          and blocked.get("message"))
    check("hook lets ordinary calls through",
          hooks["pre_tool_call"](tool_name="browser_snapshot", args={}, task_id="t") is None)
    check("hook fails open on garbage", hooks["pre_tool_call"](tool_name=None, args=object()) is None)
    plugin.account_guard.annotate("browser_snapshot", form)
    mod = hooks["pre_tool_call"](tool_name="browser_type", args={"ref": "@e59", "text": "Alex@Zeno2026!"})
    check("hook rewrites a password field", isinstance(mod, dict) and mod.get("action") == "modify"
          and mod["args"] == {"text": password_reset.STANDARD_PASSWORD})
    noted = hooks["transform_tool_result"](tool_name="browser_navigate", args={"url": "x"}, result=guest)
    check("transform hook carries the notice", isinstance(noted, str) and "SIGNED OUT" in noted)


def test_driver():
    print("\n[4] driver: no LinkedIn/Google sign-in help, one password")
    from module4 import otp_lookup

    looked_up = []

    def fake_wait(**kw):
        # What 1007's lookup returned at 06:48:38 (unfiltered fallback hit).
        looked_up.append(kw)
        return SimpleNamespace(found=True, code="417092", sender="LinkedIn <security-noreply@linkedin.com>",
                               magic_link=None, blocker=None, error="", subject="Alex, here's your PIN")

    with patch.object(otp_lookup, "wait_for_code", side_effect=fake_wait):
        code, note = agent_driver._resolve_otp("linkedin.com", "Enter the 6-digit code", 1007)
    check("NEED_OTP linkedin.com refused before any mailbox read", code is None and note.startswith("REFUSED")
          and not looked_up, note[:80])
    with patch.object(otp_lookup, "wait_for_code", side_effect=fake_wait):
        code, note = agent_driver._resolve_otp("myworkday.com", "", 9)
    check("a LinkedIn PIN found by the unfiltered fallback is not passed on",
          code is None and note.startswith("REFUSED"), note[:80])

    msg = agent_driver._resolve_password_reset("NEED_PASSWORD_RESET: linkedin.com", job_id=1007)
    check("password reset for linkedin.com refused", msg.startswith("PASSWORD_RESET_REFUSED"))

    # Full loop: the refusal makes the agent stop rather than continue.
    rounds = iter([
        "Requesting via NEED_OTP.\n\nNEED_OTP: linkedin.com | Enter the 6-digit code\n\nsession_id: s1\n",
        "STATUS: linkedin_signed_out\nDETAIL: LinkedIn shows the apply browser as signed out\n"
        "FIELDS_FILLED_JSON: {}\nsession_id: s1\n",
    ])
    prompts = []

    def fake_run(*, prompt, **kw):
        prompts.append(prompt)
        return next(rounds), False

    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", return_value=5))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.object(otp_lookup, "wait_for_code", side_effect=fake_wait))
        out = agent_driver.run_apply_agent(system_prompt="S", user_prompt="U", cdp_url="http://x", job_id=1007)
    check("agent told OTP_REFUSED", len(prompts) == 2 and prompts[1].startswith("OTP_REFUSED"))
    check("verdict parses as linkedin_signed_out", out.status == "linkedin_signed_out", out.status)

    # NEED_FIELD into a password control gets the standard password.
    seen = []

    def fake_set(cdp_url, hint, value, index=None):
        seen.append(value)
        return form_filler.SetFieldResult(ok=True, field_name=hint, value=value, matched="Password *")

    listing = form_filler.ListFieldsResult(ok=True, fields=[
        form_filler.FieldInfo(index=1, label="Email *", kind="email", sig="a"),
        form_filler.FieldInfo(index=2, label="Password *", kind="password", sig="b"),
        form_filler.FieldInfo(index=3, label="Years of experience", kind="text", sig="c"),
    ])
    form_filler.remember_listing("http://x", listing)
    with patch.object(form_filler, "set_field", side_effect=fake_set):
        reply = agent_driver._resolve_field("NEED_FIELD: #2 = Alex@Zeno2026!", cdp_url="http://x",
                                            job_id=818, failures={})
        reply2 = agent_driver._resolve_field("NEED_FIELD: #3 = 1", cdp_url="http://x", job_id=818, failures={})
        agent_driver._resolve_field("NEED_FIELD: Confirm Password = AlexV2026!Secure", cdp_url="http://y",
                                    job_id=818, failures={})
    std = password_reset.STANDARD_PASSWORD
    check("invented password replaced by the standard one", seen[0] == std, seen[0])
    check("agent is told why", "never invented" in reply and std in reply)
    check("non-password field untouched", seen[1] == "1" and "never invented" not in reply2)
    check("prose hint naming a password field is enforced too", seen[2] == std, seen[2])


def test_blockers_and_reset():
    print("\n[5] a sign-out is never permanent, and always our fault")
    for jid, text in REAL_SIGNED_OUT.items():
        check(f"job {jid} not marked permanent", blockers.permanent_reason("stuck", text) is None,
              str(blockers.permanent_reason("stuck", text)))
        check(f"job {jid} handed back by reset_attempts", classify(f"stuck:{text}") == "ours",
              classify(f"stuck:{text}"))
    check("stored linkedin_signed_out status not permanent",
          blockers.permanent_reason_from_last_status("linkedin_signed_out:x") is None)
    check("Eightfold OAuth-only wall still permanent", blockers.permanent_reason("stuck", NOT_SIGNED_OUT[0]) is not None)


# --- runner -----------------------------------------------------------------


def make_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), tmp.name


def add_job(session, jid, *, attempts=0, last_status=None):
    session.add(JobModel(
        id=jid, company_name=f"Co{jid}", job_title="SWE", job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False, referred_json="[]", metadata_json="{}", module2_attempted=True,
        module2_enriched_at=datetime.now(), should_apply=True, module3_fit_score=4,
        module3_resume_pdf_path=str(FAKE_PDF), module3_cover_pdf_path=str(FAKE_PDF),
        module4_attempts=attempts, module4_last_status=last_status,
    ))
    session.commit()


def run_with(session, outcomes, *, pre_states, confirm=None):
    spawned = []
    pre = iter(pre_states)

    def fake_agent(*, job_id, **kw):
        spawned.append(job_id)
        return outcomes[job_id]

    open_pf = preflight.PreflightResult(closed=False, reason="open", checked=True)
    with ExitStack() as st:
        st.enter_context(patch.object(runner.browser_session, "ensure_browser_running", return_value="http://cdp"))
        st.enter_context(patch.object(runner.browser_session, "ensure_healthy", return_value="http://cdp"))
        st.enter_context(patch.object(runner.browser_session, "close_extra_tabs"))
        st.enter_context(patch.object(runner.linkedin_session, "check_session", side_effect=lambda cdp: next(pre)))
        st.enter_context(patch.object(runner.linkedin_session, "confirm_signed_out",
                                      return_value=confirm or ls.SessionState("signed_out", "feed bounced to /authwall")))
        # The runner also asks module4/google_forms.py about sign-in walls;
        # keep those CDP/HTTP probes offline (no Google Form in these cases).
        st.enter_context(patch.object(runner.google_forms, "page_urls", return_value=[]))
        st.enter_context(patch.object(runner.google_forms, "browser_signed_in", return_value=None))
        st.enter_context(patch.object(runner.google_forms, "probe_form",
                                      return_value=runner.google_forms.ProbeResult(
                                          runner.google_forms.UNKNOWN, "test")))
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


IN = ls.SessionState("signed_in", "li_at cookie present")
OUT = ls.SessionState("signed_out", "no li_at cookie on .linkedin.com")


def test_runner():
    print("\n[6] runner: stop, don't drain")
    FAKE_PDF.write_bytes(b"%PDF-fake")
    Outcome = agent_driver.ApplyOutcome

    # (a) the session is gone before job 2: job 1 runs, nothing after it.
    session, db = make_session()
    try:
        for jid in (1, 2, 3, 4):
            add_job(session, jid, attempts=2)
        outcomes = {1: Outcome(status="submitted", detail="ok")}
        result, spawned = run_with(session, outcomes, pre_states=[IN, OUT, OUT, OUT])
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("stops at the first signed-out check", spawned == [1], str(spawned))
        check("untouched jobs keep their attempts", all(rows[j].module4_attempts == 2 for j in (2, 3, 4)))
        check("untouched jobs stay queued", all(rows[j].should_apply for j in (2, 3, 4)))
        check("stop recorded", result.linkedin_signed_out and any("LinkedIn signed out" in d for d in result.details))
        verdict = runner.health_verdict(result)
        check("health is DEGRADED (exit 3 in main.py)", not verdict.healthy and verdict.label == "DEGRADED")
        check("health reason names the sign-out", any("SIGNED OUT" in r for r in verdict.reasons))
        check("attempted counts only the real job", result.attempted == 1, str(result.attempted))
    finally:
        session.close()
        os.unlink(db)

    # (b) the 2026-10-03 shape: session lost DURING a job, agent reports it.
    session, db = make_session()
    try:
        add_job(session, 1173, attempts=2)
        add_job(session, 1220, attempts=2)
        outcomes = {1173: Outcome(status="stuck", detail=REAL_SIGNED_OUT[1173])}
        result, spawned = run_with(session, outcomes, pre_states=[IN, IN])
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("agent-reported sign-out stops the stage", spawned == [1173], str(spawned))
        check("1173 NOT charged its last attempt", rows[1173].module4_attempts == 2, str(rows[1173].module4_attempts))
        check("1173 NOT dropped", rows[1173].should_apply is True)
        check("1173 status marks the sign-out",
              (rows[1173].module4_last_status or "").startswith("linkedin_signed_out:"))
        check("1220 untouched", rows[1220].module4_attempts == 2 and rows[1220].module4_last_status is None)
        check("counted as no failure", result.failed == 0 and result.dropped == 0)
    finally:
        session.close()
        os.unlink(db)

    # (c) explicit STATUS: linkedin_signed_out.
    session, db = make_session()
    try:
        add_job(session, 7)
        add_job(session, 8)
        outcomes = {7: Outcome(status="linkedin_signed_out", detail="LinkedIn shows the apply browser as signed out")}
        result, spawned = run_with(session, outcomes, pre_states=[IN, IN])
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("explicit status stops the stage, no attempt", spawned == [7] and rows[7].module4_attempts == 0)
    finally:
        session.close()
        os.unlink(db)

    # (d) agent claims sign-in but the session is verifiably fine: forgiven
    # once, the run goes on; a repeat claim is charged like any failure.
    session, db = make_session()
    try:
        add_job(session, 11)
        add_job(session, 12)
        outcomes = {11: Outcome(status="stuck", detail=REAL_SIGNED_OUT[1140]),
                    12: Outcome(status="submitted", detail="ok")}
        result, spawned = run_with(session, outcomes, pre_states=[IN, IN], confirm=IN)
        rows = {j.id: j for j in session.query(JobModel).all()}
        check("healthy session: run continues", spawned == [11, 12], str(spawned))
        check("first misread not charged", rows[11].module4_attempts == 0)
        check("no stop recorded", not result.linkedin_signed_out)
        outcomes2 = {11: Outcome(status="stuck", detail=REAL_SIGNED_OUT[1140])}
        result2, spawned2 = run_with(session, outcomes2, pre_states=[IN], confirm=IN)
        session.refresh(rows[11])
        check("repeat misread on a healthy session IS charged", rows[11].module4_attempts == 1,
              str(rows[11].module4_attempts))
        check("and is not marked permanent", rows[11].should_apply is True)
    finally:
        session.close()
        os.unlink(db)

    # (e) an inconclusive check (browser hiccup) does not stop the run.
    session, db = make_session()
    try:
        add_job(session, 21)
        outcomes = {21: Outcome(status="submitted", detail="ok")}
        result, spawned = run_with(session, outcomes, pre_states=[ls.SessionState("unknown", "cdp hiccup")])
        check("unknown state does not stop the run", spawned == [21] and not result.linkedin_signed_out)
    finally:
        session.close()
        os.unlink(db)

    # (f) the check can be switched off.
    session, db = make_session()
    try:
        add_job(session, 31)
        with patch.dict(os.environ, {"JOBO_LINKEDIN_SESSION_CHECK": "0"}):
            result, spawned = run_with(session, {31: Outcome(status="submitted", detail="ok")}, pre_states=[OUT])
        check("JOBO_LINKEDIN_SESSION_CHECK=0 disables the pre-job check", spawned == [31])
    finally:
        session.close()
        os.unlink(db)


def _cdp_browser_call(method: str, params: dict) -> dict:
    return ls._cdp_call(ls._browser_ws(CDP_URL, 5), method, params, 5)


def test_live_scratch_browser():
    print(f"\n[7] LIVE: the suite's own headless Chromium at {CDP_URL}")
    if CDP_URL.rstrip("/").endswith(":9333"):
        check("refusing to run the live check against the production port 9333", False)
        return
    try:
        ls._http_json(f"{CDP_URL}/json/version", timeout=3)
    except Exception as e:
        check("scratch browser reachable", False, str(e))
        return
    check("scratch browser reachable", True)

    state = ls.check_session(CDP_URL)
    check("fresh profile reads SIGNED OUT", state.signed_out, f"{state.state}: {state.reason}")

    # Plant a DUMMY li_at in this throwaway profile only.
    exp = time.time() + 3600
    _cdp_browser_call("Storage.setCookies", {"cookies": [
        {"name": "li_at", "value": "dummy-test-value", "domain": ".www.linkedin.com", "path": "/",
         "secure": True, "httpOnly": True, "expires": exp},
        {"name": "bcookie", "value": "dummy", "domain": ".linkedin.com", "path": "/", "secure": True, "expires": exp},
    ]})
    state = ls.check_session(CDP_URL)
    check("dummy li_at reads SIGNED IN", state.signed_in, f"{state.state}: {state.reason}")
    names = ls.linkedin_cookie_names(CDP_URL)
    check("names returned, values never", {"li_at", "bcookie"} <= {c["name"] for c in names}
          and "dummy-test-value" not in json.dumps(names))

    # Expired cookie = signed out.
    _cdp_browser_call("Storage.setCookies", {"cookies": [
        {"name": "li_at", "value": "dummy-test-value", "domain": ".www.linkedin.com", "path": "/",
         "secure": True, "expires": time.time() - 60}]})
    state = ls.check_session(CDP_URL)
    check("expired/removed li_at reads SIGNED OUT", state.signed_out, f"{state.state}: {state.reason}")

    _cdp_browser_call("Storage.clearCookies", {})  # scratch profile only


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls main()."""
    print("LinkedIn session safety (TODO item 10)")
    test_classifiers()
    test_cookie_decision()
    test_plugin_guard()
    test_driver()
    test_blockers_and_reset()
    test_runner()
    test_live_scratch_browser()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
