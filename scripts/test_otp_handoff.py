"""Tests for the OTP handoff loop in module4.agent_driver.

Covers the control flow without spawning real agents or touching Gmail:
_run_hermes and _resolve_otp are monkeypatched, so what's under test is the
loop logic itself - does a NEED_OTP directive trigger a lookup, is the session
resumed, is the round cap enforced, does a verdict beat a stray directive.

Run: uv run python scripts/test_otp_handoff.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import agent_driver  # noqa: E402

VERDICT = (
    "STATUS: submitted\n"
    "DETAIL: Application submitted successfully.\n"
    'FIELDS_FILLED_JSON: {"Full Name": "Alex Example"}\n'
)


class Harness:
    """Scripted stand-in for _run_hermes plus fake OTP/upload resolvers."""

    def __init__(
        self,
        rounds: list[str],
        code: str | None = "654321",
        upload_ok: bool = True,
        field_ok: bool = True,
    ):
        self.rounds = rounds
        self.code = code
        self.upload_ok = upload_ok
        self.field_ok = field_ok
        self.calls: list[dict] = []
        self.otp_requests: list[tuple[str, str]] = []
        self.otp_not_before: list = []
        self.upload_requests: list[str] = []
        self.field_requests: list[str] = []

    def run_hermes(self, *, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        self.calls.append(
            {
                "prompt": prompt,
                "resume_session": resume_session,
                "timeout": timeout_sec,
                "max_turns": max_turns,
            }
        )
        idx = len(self.calls) - 1
        if idx >= len(self.rounds):
            return "session_id: sess_test\n" + VERDICT, False
        return self.rounds[idx], False

    def resolve_otp(self, sender, hint, job_id, not_before=None, seen=None, company="", employer_urls=(), cdp_url=""):
        self.otp_requests.append((sender, hint))
        self.otp_not_before.append(not_before)
        if self.code and seen is not None:
            seen["received_at"] = datetime.now(timezone.utc)
        if self.code:
            return self.code, f"code retrieved from {sender}"
        return None, "no code found"

    def resolve_upload(self, line, *, cdp_url, resume_path, cover_path, job_id):
        self.upload_requests.append(line)
        if self.upload_ok:
            return "UPLOAD_DONE: resume.pdf attached to id=\"resume\""
        return 'UPLOAD_FAILED: nothing accepted the file\nFile inputs that WERE found:\n  - id="other"'

    def resolve_field(self, line, *, cdp_url, job_id, failures=None):
        self.field_requests.append(line)
        if self.field_ok:
            return "FIELD_SET: College Name = 'LIIT Springfield' (via backing-hidden-select)"
        return "FIELD_SET_FAILED: no form control matched the field hint"


def run_case(
    label,
    rounds,
    code: str | None = "654321",
    expect_status="submitted",
    checks=None,
    upload_ok=True,
    field_ok=True,
    is_retry=False,
    budget_boost=False,
):
    h = Harness(rounds, code=code, upload_ok=upload_ok, field_ok=field_ok)
    orig_run, orig_otp = agent_driver._run_hermes, agent_driver._resolve_otp
    orig_upload = agent_driver._resolve_upload
    orig_field = agent_driver._resolve_field
    orig_dump = agent_driver._dump_debug_transcript
    agent_driver._run_hermes = h.run_hermes
    agent_driver._resolve_otp = h.resolve_otp
    agent_driver._resolve_upload = h.resolve_upload
    agent_driver._resolve_field = h.resolve_field
    agent_driver._dump_debug_transcript = lambda *a, **k: None
    try:
        outcome = agent_driver.run_apply_agent(
            system_prompt="SYS",
            user_prompt="TASK",
            cdp_url="http://127.0.0.1:9333",
            job_id=999,
            is_retry=is_retry,
            resume_path="/tmp/resume.pdf",
            cover_path="/tmp/cover.pdf",
            budget_boost=budget_boost,
        )
    finally:
        agent_driver._run_hermes = orig_run
        agent_driver._resolve_otp = orig_otp
        agent_driver._resolve_upload = orig_upload
        agent_driver._resolve_field = orig_field
        agent_driver._dump_debug_transcript = orig_dump

    problems = []
    if outcome.status != expect_status:
        problems.append(f"status: expected {expect_status!r}, got {outcome.status!r}")
    for desc, fn in (checks or []):
        if not fn(h, outcome):
            problems.append(desc)

    if problems:
        print(f"  FAIL  {label}")
        for p in problems:
            print(f"          {p}")
        return False
    print(f"  PASS  {label}")
    return True


def main() -> int:
    results = []

    # 1. No OTP needed: single round, straight to a verdict.
    results.append(
        run_case(
            "no_otp_single_round",
            ["session_id: s1\n" + VERDICT],
            checks=[
                ("should have made exactly 1 agent call", lambda h, o: len(h.calls) == 1),
                ("should not have asked for a code", lambda h, o: not h.otp_requests),
            ],
        )
    )

    # 2. One OTP round: directive -> code injected -> resumed -> verdict.
    results.append(
        run_case(
            "single_otp_round",
            [
                "session_id: s2\nClicked send code.\nNEED_OTP: northwind.com\n",
                VERDICT,
            ],
            checks=[
                ("should have made 2 agent calls", lambda h, o: len(h.calls) == 2),
                (
                    "should have requested a code for northwind.com",
                    lambda h, o: h.otp_requests == [("northwind.com", "")],
                ),
                (
                    "second call must resume session s2",
                    lambda h, o: h.calls[1]["resume_session"] == "s2",
                ),
                (
                    "second prompt must carry the code",
                    lambda h, o: "VERIFICATION_CODE: 654321" in h.calls[1]["prompt"],
                ),
                (
                    # Only mail that arrived after the request turn began may
                    # supply the code (job 1007 reused stale mail).
                    "lookup must get a not_before timestamp from this turn",
                    lambda h, o: len(h.otp_not_before) == 1
                    and h.otp_not_before[0] is not None
                    and 0
                    <= (datetime.now(timezone.utc) - h.otp_not_before[0]).total_seconds()
                    < 600,
                ),
            ],
        )
    )

    # 3. Subject hint is parsed out of the directive.
    results.append(
        run_case(
            "otp_with_subject_hint",
            [
                "session_id: s3\nNEED_OTP: myworkday.com | Verify your email\n",
                VERDICT,
            ],
            checks=[
                (
                    "hint should be parsed",
                    lambda h, o: h.otp_requests == [("myworkday.com", "Verify your email")],
                ),
            ],
        )
    )

    # 4. Code never arrives: agent is told, then reports stuck.
    results.append(
        run_case(
            "otp_never_arrives",
            [
                "session_id: s4\nNEED_OTP: adatum.com\n",
                "STATUS: stuck\nDETAIL: Code never arrived.\nFIELDS_FILLED_JSON: {}\n",
            ],
            code=None,
            expect_status="stuck",
            checks=[
                (
                    "agent must be told no code was found",
                    lambda h, o: "No verification code could be retrieved" in h.calls[1]["prompt"],
                ),
            ],
        )
    )

    # 5. Round cap: a looping agent gets cut off, not allowed to spin forever.
    results.append(
        run_case(
            "otp_round_cap_enforced",
            [
                "session_id: s5\nNEED_OTP: a.com\n",
                "NEED_OTP: a.com\n",
                "NEED_OTP: a.com\n",
                "NEED_OTP: a.com\n",
                "STATUS: stuck\nDETAIL: Gave up on codes.\nFIELDS_FILLED_JSON: {}\n",
            ],
            expect_status="stuck",
            checks=[
                (
                    f"must not exceed {agent_driver.MAX_OTP_ROUNDS} lookups",
                    lambda h, o: len(h.otp_requests) <= agent_driver.MAX_OTP_ROUNDS,
                ),
                (
                    "must eventually tell the agent to stop asking",
                    lambda h, o: any(
                        "request limit reached" in c["prompt"] for c in h.calls
                    ),
                ),
                (
                    # After a code is handed out, the next round must only
                    # accept newer mail: job 1007 was handed the same wrong
                    # code twice.
                    "each later lookup must start after the previous code's mail",
                    lambda h, o: all(
                        b is not None and a is not None and b > a
                        for a, b in zip(h.otp_not_before, h.otp_not_before[1:])
                    ),
                ),
            ],
        )
    )

    # 6. A verdict in the same output as a directive wins (no lookup at all).
    results.append(
        run_case(
            "verdict_beats_directive",
            ["session_id: s6\nNEED_OTP: x.com\n" + VERDICT],
            checks=[
                ("verdict should end the job immediately", lambda h, o: len(h.calls) == 1),
                ("should not have asked for a code", lambda h, o: not h.otp_requests),
            ],
        )
    )

    # 7. Missing session_id: cannot resume, so bail rather than restart blind.
    results.append(
        run_case(
            "no_session_id_cannot_resume",
            ["NEED_OTP: x.com\n(no session id printed)\n"],
            expect_status="stuck",
            checks=[
                ("should not have asked for a code", lambda h, o: not h.otp_requests),
                ("should have stopped after 1 call", lambda h, o: len(h.calls) == 1),
            ],
        )
    )

    # --- NEED_UPLOAD handoff ---
    print("\n  -- NEED_UPLOAD (deterministic file upload) --")

    # 8. Basic upload round: directive -> driver uploads -> resumed -> verdict.
    results.append(
        run_case(
            "upload_single_round",
            [
                "session_id: u1\nReached the resume field.\nNEED_UPLOAD: resume\n",
                VERDICT,
            ],
            checks=[
                ("should have made 2 agent calls", lambda h, o: len(h.calls) == 2),
                ("should have performed 1 upload", lambda h, o: len(h.upload_requests) == 1),
                (
                    "second call must resume session u1",
                    lambda h, o: h.calls[1]["resume_session"] == "u1",
                ),
                (
                    "agent must be told the upload is done",
                    lambda h, o: "UPLOAD_DONE" in h.calls[1]["prompt"],
                ),
            ],
        )
    )

    # 9. Cover letter slot is passed through distinctly.
    results.append(
        run_case(
            "upload_cover_slot",
            ["session_id: u2\nNEED_UPLOAD: cover | input#cover_letter\n", VERDICT],
            checks=[
                (
                    "cover directive should reach the resolver verbatim",
                    lambda h, o: "cover" in h.upload_requests[0].lower(),
                ),
            ],
        )
    )

    # 10. Resume + cover in sequence (two separate slots on one form).
    results.append(
        run_case(
            "upload_two_slots",
            [
                "session_id: u3\nNEED_UPLOAD: resume\n",
                "NEED_UPLOAD: cover\n",
                VERDICT,
            ],
            checks=[
                ("should have performed 2 uploads", lambda h, o: len(h.upload_requests) == 2),
                ("should have made 3 agent calls", lambda h, o: len(h.calls) == 3),
            ],
        )
    )

    # 11. Failed upload: agent is told, and can still finish or report.
    results.append(
        run_case(
            "upload_failure_reported",
            [
                "session_id: u4\nNEED_UPLOAD: resume\n",
                "STATUS: stuck\nDETAIL: upload slot never accepted.\nFIELDS_FILLED_JSON: {}\n",
            ],
            upload_ok=False,
            expect_status="stuck",
            checks=[
                (
                    "agent must see the failure and the candidate list",
                    lambda h, o: "UPLOAD_FAILED" in h.calls[1]["prompt"],
                ),
            ],
        )
    )

    # 12. Upload round cap: a looping agent gets cut off.
    results.append(
        run_case(
            "upload_round_cap_enforced",
            [
                "session_id: u5\nNEED_UPLOAD: resume\n",
                "NEED_UPLOAD: resume\n",
                "NEED_UPLOAD: resume\n",
                "NEED_UPLOAD: resume\n",
                "NEED_UPLOAD: resume\n",
                "STATUS: stuck\nDETAIL: gave up.\nFIELDS_FILLED_JSON: {}\n",
            ],
            expect_status="stuck",
            checks=[
                (
                    f"must not exceed {agent_driver.MAX_UPLOAD_ROUNDS} uploads",
                    lambda h, o: len(h.upload_requests) <= agent_driver.MAX_UPLOAD_ROUNDS,
                ),
                (
                    "must eventually tell the agent to stop asking",
                    lambda h, o: any("request limit reached" in c["prompt"] for c in h.calls),
                ),
            ],
        )
    )

    # 13. Upload takes priority when both directives appear (file first, then verify).
    results.append(
        run_case(
            "upload_precedes_otp",
            [
                "session_id: u6\nNEED_UPLOAD: resume\nNEED_OTP: example.com\n",
                VERDICT,
            ],
            checks=[
                ("upload should run", lambda h, o: len(h.upload_requests) == 1),
                ("otp should NOT run first", lambda h, o: not h.otp_requests),
            ],
        )
    )

    # 14. A verdict still beats an upload directive.
    results.append(
        run_case(
            "verdict_beats_upload_directive",
            ["session_id: u7\nNEED_UPLOAD: resume\n" + VERDICT],
            checks=[
                ("verdict ends the job immediately", lambda h, o: len(h.calls) == 1),
                ("no upload should be performed", lambda h, o: not h.upload_requests),
            ],
        )
    )

    # --- Retry budget (the inversion fix) ---
    print("\n  -- NEED_FIELD (deterministic dropdown/field setting) --")

    results.append(
        run_case(
            "field_single_round",
            [
                "session_id: f1\nThe college dropdown won't take a value.\n"
                "NEED_FIELD: College Name = LIIT Springfield\n",
                VERDICT,
            ],
            checks=[
                ("should have performed 1 field set", lambda h, o: len(h.field_requests) == 1),
                (
                    "second call must resume session f1",
                    lambda h, o: h.calls[1]["resume_session"] == "f1",
                ),
                (
                    "agent must be told the field is set",
                    lambda h, o: "FIELD_SET" in h.calls[1]["prompt"],
                ),
            ],
        )
    )

    results.append(
        run_case(
            "field_multiple_rounds",
            [
                "session_id: f2\nNEED_FIELD: College Name = LIIT Springfield\n",
                "NEED_FIELD: Country = India\n",
                "NEED_FIELD: Notice Period = 30 days\n",
                VERDICT,
            ],
            checks=[
                ("should have performed 3 field sets", lambda h, o: len(h.field_requests) == 3),
                ("should have made 4 agent calls", lambda h, o: len(h.calls) == 4),
            ],
        )
    )

    results.append(
        run_case(
            "field_failure_reported",
            [
                "session_id: f3\nNEED_FIELD: Nonexistent = x\n",
                "STATUS: stuck\nDETAIL: field never settable.\nFIELDS_FILLED_JSON: {}\n",
            ],
            field_ok=False,
            expect_status="stuck",
            checks=[
                (
                    "agent must see the failure",
                    lambda h, o: "FIELD_SET_FAILED" in h.calls[1]["prompt"],
                ),
            ],
        )
    )

    results.append(
        run_case(
            "upload_precedes_field",
            [
                "session_id: f4\nNEED_UPLOAD: resume\nNEED_FIELD: Country = India\n",
                VERDICT,
            ],
            checks=[
                ("upload runs first", lambda h, o: len(h.upload_requests) == 1),
                ("field does not run in the same round", lambda h, o: not h.field_requests),
            ],
        )
    )

    results.append(
        run_case(
            "verdict_beats_field_directive",
            ["session_id: f5\nNEED_FIELD: Country = India\n" + VERDICT],
            checks=[
                ("verdict ends the job", lambda h, o: len(h.calls) == 1),
                ("no field set performed", lambda h, o: not h.field_requests),
            ],
        )
    )

    # --- Retry budget (the inversion fix) ---
    print("\n  -- retry budget --")

    # 15. A plain retry keeps the reduced budget.
    results.append(
        run_case(
            "plain_retry_gets_reduced_budget",
            ["session_id: b1\n" + VERDICT],
            is_retry=True,
            budget_boost=False,
            checks=[
                (
                    f"max_turns should be {agent_driver.AGENT_STEPS_RETRY}",
                    lambda h, o: h.calls[0]["max_turns"] == str(agent_driver.AGENT_STEPS_RETRY),
                ),
                (
                    "timeout should be the retry timeout",
                    lambda h, o: h.calls[0]["timeout"] <= agent_driver.AGENT_TIMEOUT_RETRY_SEC,
                ),
            ],
        )
    )

    # 16. A retry after budget exhaustion gets an ESCALATED budget - the whole point.
    results.append(
        run_case(
            "boosted_retry_gets_escalated_budget",
            ["session_id: b2\n" + VERDICT],
            is_retry=True,
            budget_boost=True,
            checks=[
                (
                    f"max_turns should be {agent_driver.AGENT_STEPS_ESCALATED}",
                    lambda h, o: h.calls[0]["max_turns"] == str(agent_driver.AGENT_STEPS_ESCALATED),
                ),
                (
                    "turns must EXCEED the first-attempt budget, not just match it",
                    lambda h, o: int(h.calls[0]["max_turns"])
                    > agent_driver.AGENT_STEPS_FIRST,
                ),
                (
                    "timeout should exceed the first-attempt timeout",
                    lambda h, o: h.calls[0]["timeout"] > agent_driver.AGENT_TIMEOUT_FIRST_SEC - 1,
                ),
            ],
        )
    )

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
