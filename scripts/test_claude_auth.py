#!/usr/bin/env python3
"""An expired Claude login stops the run instead of failing every call (GHO-77).

On 22 Oct the host's Claude OAuth login had expired before the 10:00 run. The
budget gate called it "usage unreadable" and proceeded, triage made 462 calls
(3 per job) that all answered "Failed to authenticate: OAuth session expired
and could not be refreshed" and still said OK, and run-report only saw
"apply FAILED". The responses below are the ones that run logged; no real
`claude` call is made (subprocess.run and the probe are faked).

  1. the auth probe classifies claude's answers
  2. check_auth: a valid token needs no call; an expired one is probed
  3. claude_client raises ClaudeAuthError at once (no 3x retry)
  4. triage stops after AUTH_FAILURE_LIMIT auth failures and says FAILED
  5. `jobo triage` / `jobo budget-gate` exit 77 (daily_pipeline.sh stops)
  6. run-report names the cause and keeps stage:claude_auth @ pipeline

scripts/test_pipeline_budget_gate.sh drives daily_pipeline.sh through the same
exit statuses.

Run: bash scripts/run_regression_tests.sh test_claude_auth
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from click.testing import CliRunner  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import main as jobo_main  # noqa: E402
from db.models import Base, FailureLedgerModel, JobModel  # noqa: E402
from module3 import claude_auth, claude_client, runner  # noqa: E402
from module3.runner import Module3Result  # noqa: E402
from module3.schema import FitDecisionOutput  # noqa: E402
from module4 import budget_guard  # noqa: E402
from module4 import run_report as rr  # noqa: E402

results: list[tuple[str, bool, str]] = []

# What `claude -p --output-format json` printed for every call on 22 Oct.
EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"
EXPIRED_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "result": EXPIRED,
    "usage": {"input_tokens": 0, "output_tokens": 0},
})
OK_ENVELOPE = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "OK"})
FAKE_TOKEN = "sk-ant-oat-fake-token-must-never-be-printed"
PROFILE_PATH = REPO / "scripts" / "fixtures" / "module3_profile.md"


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:72s} {note}")


def write_creds(home: Path, *, expires_in_h: float | None, refresh: bool = True) -> None:
    oauth = {"accessToken": FAKE_TOKEN}
    if expires_in_h is not None:
        oauth["expiresAt"] = int((time.time() + expires_in_h * 3600) * 1000)
    if refresh:
        oauth["refreshToken"] = "fake-refresh"
        oauth["refreshTokenExpiresAt"] = int((time.time() - 3600) * 1000)
    path = budget_guard.credentials_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


def section_probe() -> None:
    print("1. the auth probe reads claude's answer")
    ok, why = claude_auth.classify_probe(1, EXPIRED_ENVELOPE, "")
    check("22 Oct envelope -> auth failed, with the reason", ok is False and why == EXPIRED, why)
    check("a normal answer -> auth works", claude_auth.classify_probe(0, OK_ENVELOPE, "")[0] is True)
    ok, why = claude_auth.classify_probe(1, "", "Not logged in · Please run /login")
    check("plain-text 'Please run /login' on stderr -> auth failed", ok is False, why)
    ok, why = claude_auth.classify_probe(
        1, json.dumps({"is_error": True, "subtype": "error", "result": "API Error: 529 Overloaded"}), "")
    check("an outage is not an auth failure (unknown, proceed)", ok is None, why)
    check("a timeout is unknown too", claude_auth.classify_probe(1, "", "connect ETIMEDOUT")[0] is None)
    for text in ("Failed to authenticate. API Error: 401 {\"type\":\"authentication_error\"}",
                 "OAuth token has expired. Please obtain a new token or refresh your existing token.",
                 "Invalid API key · Please run /login"):
        check(f"auth text: {text[:40]!r}", claude_auth.is_auth_error(text))
    for text in ("stuck:Workday sign-in required, failed to authenticate with the tenant SSO",
                 "HEALTH: LinkedIn is SIGNED OUT in the apply browser",
                 "signin:oauth_only Google account required",
                 "weekly Claude usage UNREADABLE (no unexpired OAuth token in /x/.credentials.json)"):
        check(f"not Claude auth: {text[:40]!r}", not claude_auth.is_auth_error(text))

    # probe_claude's own command: lean, no tools, nothing from the repo.
    seen = {}

    def fake_run(cmd, *, input, capture_output, text, timeout, cwd):
        seen.update(cmd=cmd, cwd=cwd)
        return SimpleNamespace(returncode=1, stdout=EXPIRED_ENVELOPE, stderr="")

    saved = (claude_auth.subprocess, claude_client._claude_bin)
    claude_auth.subprocess = SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    claude_client._claude_bin = lambda: "/usr/bin/claude"
    try:
        ok, why = claude_auth.probe_claude()
    finally:
        claude_auth.subprocess, claude_client._claude_bin = saved
    cmd = seen.get("cmd") or []
    check("probe_claude: auth failure comes back", ok is False and why == EXPIRED, why)
    check("probe: cheap model, tools off, no settings",
          cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--tools") + 1] == ""
          and cmd[cmd.index("--setting-sources") + 1] == "", " ".join(cmd[:4]))
    check("probe runs outside the repo", not str(seen.get("cwd", "")).startswith(str(REPO)))


def section_check_auth(tmp: Path) -> None:
    print("\n2. check_auth(): free when the token is valid, probed when not")
    calls = []

    def probe(result):
        def _p():
            calls.append(1)
            return result
        return _p

    fresh = tmp / "fresh"
    write_creds(fresh, expires_in_h=5)
    a = claude_auth.check_auth(fresh, probe=probe((False, EXPIRED)))
    check("valid token -> OK with no call", a.decision == "OK" and not calls and not a.probed)
    check("OK writes no log line", claude_auth.gate_message(a, "pipeline") == "")

    expired = tmp / "expired"
    write_creds(expired, expires_in_h=-5.25)
    a = claude_auth.check_auth(expired, probe=probe((False, EXPIRED)))
    msg = claude_auth.gate_message(a, "pipeline")
    check("expired token + auth error -> FAILED", a.failed and a.decision == "FAILED" and len(calls) == 1)
    check("detail says the token expired and the refresh token too",
          "expired at" in a.detail and "refresh token expired" in a.detail and EXPIRED in a.detail, a.detail[:90])
    check("log line names claude_auth and the fix",
          "FAILED" in msg and "claude_auth" in msg and "claude /login" in msg, msg[:80])
    check("the token itself is never in the detail or record",
          FAKE_TOKEN not in a.detail and FAKE_TOKEN not in json.dumps(a.record()))

    a = claude_auth.check_auth(expired, probe=probe((True, "auth probe answered")))
    check("expired token, probe refreshes it -> PROBED_OK, run goes on", a.decision == "PROBED_OK" and not a.failed)
    a = claude_auth.check_auth(expired, probe=probe((None, "auth probe timed out after 90s")))
    check("probe inconclusive -> UNKNOWN, run goes on", a.decision == "UNKNOWN" and not a.failed)
    check("UNKNOWN log line says proceeding",
          "proceeding" in claude_auth.gate_message(a, "triage"))

    a = claude_auth.check_auth(tmp / "nobody", probe=probe((False, "Not logged in · Please run /login")))
    check("no credentials file (e.g. macOS keychain) -> probed, not failed on sight",
          a.failed and a.probed and "no credentials file" in a.detail, a.detail[:60])
    check("budget error text unchanged for a missing token",
          "no unexpired OAuth token" in (budget_guard.check_budget(host_home=tmp / "nobody").error or ""))


def section_client() -> None:
    print("\n3. claude_client: an auth failure is not retried")
    calls = []

    def fake_run(cmd, *, input, capture_output, text, timeout, cwd):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout=EXPIRED_ENVELOPE, stderr="")

    saved = (claude_client.subprocess, claude_client._claude_bin, claude_client.time.sleep)
    claude_client.subprocess = SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    claude_client._claude_bin = lambda: "/usr/bin/claude"
    claude_client.time.sleep = lambda _s: None
    try:
        try:
            claude_client.generate_structured(
                system_instruction="S", user_text="U", response_model=FitDecisionOutput)
            raised = None
        except Exception as e:  # noqa: BLE001
            raised = e
        check("ClaudeAuthError raised", isinstance(raised, claude_auth.ClaudeAuthError), repr(raised)[:80])
        check("after one call, not three", len(calls) == 1, f"{len(calls)} calls")
        check("message keeps claude's words", EXPIRED in str(raised))

        calls.clear()

        def stderr_only(cmd, *, input, capture_output, text, timeout, cwd):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="Invalid API key · Please run /login")

        claude_client.subprocess = SimpleNamespace(run=stderr_only, TimeoutExpired=subprocess.TimeoutExpired)
        try:
            claude_client.generate_structured(system_instruction="S", user_text="U", response_model=FitDecisionOutput)
        except claude_auth.ClaudeAuthError:
            pass
        check("auth error on stderr with no stdout: one call", len(calls) == 1, f"{len(calls)} calls")

        calls.clear()

        def overloaded(cmd, *, input, capture_output, text, timeout, cwd):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout=json.dumps(
                {"is_error": True, "subtype": "error", "result": "overloaded"}), stderr="")

        claude_client.subprocess = SimpleNamespace(run=overloaded, TimeoutExpired=subprocess.TimeoutExpired)
        try:
            claude_client.generate_structured(system_instruction="S", user_text="U", response_model=FitDecisionOutput)
        except RuntimeError as e:
            check("other errors still retried 3x, not as auth errors",
                  len(calls) == claude_client.MAX_ATTEMPTS and not isinstance(e, claude_auth.ClaudeAuthError),
                  f"{len(calls)} calls")
    finally:
        claude_client.subprocess, claude_client._claude_bin, claude_client.time.sleep = saved


def make_session(tmp: Path, name: str):
    engine = create_engine(f"sqlite:///{tmp / name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_triage_jobs(session, ids) -> None:
    for jid in ids:
        session.add(JobModel(
            id=jid, company_name=f"Co{jid}", job_title=f"Backend Engineer {jid}",
            job_url=f"https://www.linkedin.com/jobs/view/{jid}/", applied=False,
            referred_json="[]", metadata_json=json.dumps({"stack": ["Python"]}),
            module2_attempted=True, module2_enriched_at=datetime.now(), module4_attempts=0,
        ))
    session.commit()


def run_triage(session, fake_llm, workers: str = "12") -> Module3Result:
    keys = ("JOBO_BASE_RESUME_TEX", "JOBO_DEFAULT_RESUME_PDF", "JOBO_ARTIFACTS_DIR",
            "JOBO_TRIAGE_WORKERS", "JOBO_LLM_BACKEND", "JOBO_PROFILE_CONTEXT_PATH")
    saved_env = {k: os.environ.get(k) for k in keys}
    saved = (runner.llm.generate_structured, runner.preflight.check_job_open)
    art = tempfile.TemporaryDirectory()
    os.environ.update(
        JOBO_BASE_RESUME_TEX=str(REPO / "personal.example" / "resume.tex"),
        JOBO_DEFAULT_RESUME_PDF=str(REPO / "personal.example" / "resume.pdf"),
        JOBO_ARTIFACTS_DIR=art.name, JOBO_TRIAGE_WORKERS=workers,
        JOBO_PROFILE_CONTEXT_PATH=str(PROFILE_PATH),
    )
    os.environ.pop("JOBO_LLM_BACKEND", None)
    runner.llm.generate_structured = fake_llm
    runner.preflight.check_job_open = lambda url: SimpleNamespace(should_skip=False, reason="open")
    try:
        return runner.run_module3(session)
    finally:
        runner.llm.generate_structured, runner.preflight.check_job_open = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        art.cleanup()


def section_runner(tmp: Path) -> None:
    print("\n4. triage: stop after a few auth failures, verdict FAILED (claude_auth)")
    calls = []
    lock = threading.Lock()

    def dead_login(*, system_instruction, user_text, response_model, model=None):
        with lock:
            calls.append(response_model.__name__)
        time.sleep(0.01)
        raise claude_auth.ClaudeAuthError(f"claude returned an error (success): {EXPIRED}")

    session = make_session(tmp, "dead.db")
    add_triage_jobs(session, range(2283, 2283 + 154))
    result = run_triage(session, dead_login)
    health = runner.triage_health(result)
    check("154 jobs, login dead from the start: AUTH_FAILURE_LIMIT calls, not 462",
          len(calls) == runner.AUTH_FAILURE_LIMIT, f"{len(calls)} calls")
    check("stage stopped on the auth error", EXPIRED in result.auth_error, result.auth_error[:60])
    check("3 failed, 151 left for the next run",
          result.failed == 3 and result.auth_skipped == 151, f"{result.failed}/{result.auth_skipped}")
    check("no row got a fit score",
          session.query(JobModel).filter(JobModel.module3_fit_score.isnot(None)).count() == 0)
    check("health FAILED (claude_auth), exit 77",
          health.label == "FAILED (claude_auth)" and health.exit_code == claude_auth.AUTH_EXIT, health.label)
    check("health reasons say how many and the fix",
          any("151 job(s) left" in r for r in health.reasons) and any("claude /login" in r for r in health.reasons))

    # The login dies part way through: warm-up and early calls work, then the
    # rest of the queue is cancelled after the limit instead of all failing.
    calls.clear()
    good = set(range(5000, 5004))

    def dies_midway(*, system_instruction, user_text, response_model, model=None):
        jid = int(user_text.split("Backend Engineer ", 1)[1].split()[0])
        with lock:
            calls.append(jid)
        time.sleep(0.02)
        if jid in good:
            return FitDecisionOutput(fit_score=1, reasoning="r", highlighted_project_names=[])
        raise claude_auth.ClaudeAuthError(EXPIRED)

    session = make_session(tmp, "midway.db")
    add_triage_jobs(session, range(5000, 5100))
    result = run_triage(session, dies_midway, workers="2")
    health = runner.triage_health(result)
    check("login dies mid-stage: queued calls cancelled", len(calls) < 20, f"{len(calls)} calls of 100")
    check("jobs scored before it died are kept", result.phase1_run == 4, str(result.phase1_run))
    check("every job is accounted for",
          result.phase1_run + result.failed + result.auth_skipped == 100,
          f"{result.phase1_run}+{result.failed}+{result.auth_skipped}")
    check("still FAILED (claude_auth)", health.exit_code == claude_auth.AUTH_EXIT, health.label)

    calls.clear()
    session = make_session(tmp, "one.db")
    add_triage_jobs(session, [7001])
    result = run_triage(session, dead_login)
    check("a single job failing on the login is claude_auth too",
          runner.triage_health(result).exit_code == claude_auth.AUTH_EXIT and len(calls) == 1)

    def flaky(*, system_instruction, user_text, response_model, model=None):
        raise RuntimeError("Claude structured call failed after 3 attempts: timeout after 300s")

    session = make_session(tmp, "flaky.db")
    add_triage_jobs(session, range(8000, 8005))
    result = run_triage(session, flaky)
    health = runner.triage_health(result)
    check("every call failed (not auth) -> FAILED, exit 4",
          health.label == "FAILED" and health.exit_code == 4 and not result.auth_error, health.label)

    check("some failures, some scored -> OK",
          runner.triage_health(Module3Result(attempted=10, phase1_run=8, failed=2)).exit_code == 0)
    check("nothing to do -> OK", runner.triage_health(Module3Result()).exit_code == 0)


def section_cli() -> None:
    print("\n5. `jobo triage` and `jobo budget-gate` exit 77 on a dead login")
    cli = CliRunner()
    stopped = Module3Result(attempted=154, failed=3, auth_failures=3, auth_skipped=151,
                            auth_error=f"claude returned an error (success): {EXPIRED}")
    saved = (jobo_main.run_module3, jobo_main.init_db, jobo_main.get_session)
    jobo_main.run_module3 = lambda session, **kw: stopped
    jobo_main.init_db, jobo_main.get_session = (lambda: None), (lambda: None)
    saved_personal = None
    try:
        from policy import personal
        saved_personal = personal.require_real
        personal.require_real = lambda _stage: None
        out = cli.invoke(jobo_main.cli, ["triage", "--quiet"])
        check("triage exits 77", out.exit_code == claude_auth.AUTH_EXIT, f"exit {out.exit_code}")
        check("triage prints HEALTH: FAILED (claude_auth)", "HEALTH: FAILED (claude_auth)" in out.output)
        check("triage prints the auth counts", "Claude auth failures: 3; left for the next run: 151" in out.output)
        jobo_main.run_module3 = lambda session, **kw: Module3Result(attempted=5, phase1_run=5)
        out = cli.invoke(jobo_main.cli, ["triage", "--quiet"])
        check("a healthy triage still exits 0", out.exit_code == 0 and "HEALTH: OK" in out.output,
              f"exit {out.exit_code}")
    finally:
        jobo_main.run_module3, jobo_main.init_db, jobo_main.get_session = saved
        if saved_personal is not None:
            personal.require_real = saved_personal

    saved_auth = (claude_auth.check_auth, budget_guard.check_budget)
    budget_calls = []

    def fake_budget(**kw):
        budget_calls.append(kw)
        return budget_guard.decide(0.20, 0.50)

    budget_guard.check_budget = fake_budget
    try:
        claude_auth.check_auth = lambda: claude_auth.AuthCheck(
            "FAILED", f"OAuth access token expired; probe call: {EXPIRED}", probed=True)
        out = cli.invoke(jobo_main.cli, ["budget-gate", "--stage", "pipeline"])
        rec = json.loads(out.stdout.strip().splitlines()[-1])
        check("budget-gate exits 77 on a dead login", out.exit_code == claude_auth.AUTH_EXIT, f"exit {out.exit_code}")
        check("its JSON says FAILED_CLAUDE_AUTH with the detail",
              rec["decision"] == "FAILED_CLAUDE_AUTH" and EXPIRED in rec["claude_auth"]["detail"])
        check("the usage endpoint is not read", not budget_calls)
        check("stderr line names claude_auth", "CLAUDE AUTH GATE (pipeline): FAILED" in out.stderr)
        with_override = cli.invoke(jobo_main.cli, ["budget-gate", "--stage", "triage", "--override"])
        check("--budget-override does not lift it", with_override.exit_code == claude_auth.AUTH_EXIT)

        claude_auth.check_auth = lambda: claude_auth.AuthCheck("PROBED_OK", "refreshed", probed=True)
        out = cli.invoke(jobo_main.cli, ["budget-gate", "--stage", "pipeline"])
        rec = json.loads(out.stdout.strip().splitlines()[-1])
        check("a refreshed login goes on to the budget read (exit 0, PROCEED)",
              out.exit_code == 0 and rec["decision"] == "PROCEED" and budget_calls, f"exit {out.exit_code}")
        check("the JSON records the auth check", rec["claude_auth"]["decision"] == "PROBED_OK")
    finally:
        claude_auth.check_auth, budget_guard.check_budget = saved_auth


def write_incident_log(path: Path) -> Path:
    """The shape of run_20261022_1000.log, shortened, ids made up."""
    fit = ("WARNING:module3.runner:Fit failed for job_id={j}: Claude structured call failed after 3 attempts: "
           f"claude returned an error (success): {EXPIRED}")
    lines = [
        "[2026-01-02 10:00:01] BUDGET GATE (pipeline): weekly Claude usage UNREADABLE (no unexpired OAuth token "
        "in /home/x/.claude/.credentials.json) - proceeding as before; apply still re-checks between jobs.",
        "[2026-01-02 10:00:01] === collector starting ===",
        "[2026-01-02 10:05:59] === collector OK ===",
        "[2026-01-02 10:19:43] === triage starting ===",
        f"WARNING:module3.claude_client:Claude call failed (attempt 1/3): claude returned an error (success): {EXPIRED}",
        fit.format(j=11), fit.format(j=12), fit.format(j=13),
        "[2026-01-02 10:23:06] === triage OK ===",
        "[2026-01-02 10:23:08] === apply starting ===",
        "2026-01-02 10:23:16,671 - [li] module4.runner - ERROR - ATTACH CANARY FAILED: attach canary FAILED on "
        "worker li (CDP http://127.0.0.1:9333): agent output tail: at a logged-in config directory, then try "
        f"again. (native: {EXPIRED[:50]}",
        "[2026-01-02 10:23:22] !!! apply FAILED (exit 4) !!!",
        "[2026-01-02 10:23:22] === run complete WITH PROBLEMS, failed=1 - read the stage lines above ===",
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def section_run_report(tmp: Path) -> None:
    print("\n6. run-report shows the auth failure as the cause")
    session = make_session(tmp, "report.db")
    log = write_incident_log(tmp / "run_20260102_1000.log")
    verdicts = tmp / "pipeline_verdicts.jsonl"
    verdicts.write_text(json.dumps({
        "ts": "2026-01-02T04:53:22Z", "overall": "FAILED",
        "stages": [{"stage": "triage", "verdict": "OK"}, {"stage": "apply", "verdict": "FAILED"}],
        "log": str(log.resolve()),
    }) + "\n")
    report = rr.build_report(session, log)
    cause = report.causes[0] if report.causes else None
    check("an old-style log (no claude_auth verdict) still gets the cause",
          cause is not None and cause.signature == "stage:claude_auth" and cause.ats_host == "pipeline")
    check("cause names triage and apply, and the unscored jobs",
          cause is not None and cause.stages == ["triage", "apply"] and cause.job_ids == [11, 12, 13],
          str(cause and (cause.stages, cause.job_ids)))
    check("the budget gate's 'no unexpired OAuth token' line is not counted",
          cause is not None and "pipeline" not in cause.stages and "gate" not in cause.stages)
    text = rr.render_text(report)
    check("text report: CAUSE line with the sample and the fix",
          "CAUSE: Claude can't authenticate (stage:claude_auth @ pipeline) in triage, apply" in text
          and EXPIRED in text and "claude /login" in text, text.splitlines()[2][:80])
    check("suggested bucket needs_g", cause is not None and cause.suggested_bucket == "needs_g")
    entry = session.get(FailureLedgerModel, ("stage:claude_auth", "pipeline"))
    check("ledger row stage:claude_auth @ pipeline recorded",
          entry is not None and entry.seen_runs == 1 and entry.job_count == 3)
    rr.build_report(session, log)
    session.refresh(entry)
    check("re-reporting the same log keeps seen_runs at 1", entry.seen_runs == 1)
    check("--json carries the cause", json.loads(json.dumps(report.to_json(), default=str))["causes"][0]["signature"]
          == "stage:claude_auth")

    # A new-style run: the gate stopped it, the log has only the gate line.
    log2 = tmp / "run_20260102_2200.log"
    log2.write_text(
        "[2026-01-02 22:00:01] CLAUDE AUTH GATE (pipeline): FAILED - Claude can't authenticate "
        f"(OAuth access token expired; probe call: {EXPIRED}).\n"
        "[2026-01-02 22:00:05] === run stopped: Claude can't authenticate (claude_auth). ===\n"
    )
    with verdicts.open("a") as f:
        f.write(json.dumps({
            "ts": "2026-01-02T16:30:05Z", "overall": "FAILED_CLAUDE_AUTH",
            "stages": [{"stage": "claude_auth", "verdict": "FAILED"}],
            "budget": {"gate": "pipeline", "decision": "FAILED_CLAUDE_AUTH"}, "log": str(log2.resolve()),
        }) + "\n")
    r2 = rr.build_report(session, log2)
    c2 = r2.causes[0] if r2.causes else None
    check("gate-stopped run: cause from the verdict and the gate line",
          c2 is not None and c2.stages == ["gate"], str(c2 and c2.stages))
    check("second run is a repeat of the first", c2 is not None and c2.repeat and c2.repeat_runs == 1)
    session.refresh(entry)
    check("ledger counts 2 runs", entry.seen_runs == 2, str(entry.seen_runs))

    clean = tmp / "run_20260103_1000.log"
    clean.write_text("[2026-01-03 10:00:01] === collector starting ===\n"
                     "[2026-01-03 10:00:02] === apply starting ===\n"
                     "x - [f1] module4.runner - INFO - [5] Applying (attempt 1/3, first try): Engineer\n")
    check("a clean run has no cause", rr.build_report(session, clean).causes == [])


def main() -> int:
    section_probe()
    with tempfile.TemporaryDirectory() as tmp:
        section_check_auth(Path(tmp))
        section_client()
        section_runner(Path(tmp))
        section_cli()
        section_run_report(Path(tmp))
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
    sys.exit(main())
