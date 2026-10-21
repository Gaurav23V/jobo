#!/usr/bin/env python3
"""Apply by email for mailto-only postings (GHO-67).

Natton AI 1684 and DawerOne 1844 had no web form, only a mailto link, and
ended `stuck`. The verdict, the email (drafted or sent through a fake Gmail
service, never the real one) and the runner's bookkeeping are checked here,
using the two jobs' real agent output.

Run: uv run python scripts/test_email_apply.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from contextlib import ExitStack
from email import message_from_bytes, policy as email_policy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("JOBO_UPLOAD_COPY_DIR", tempfile.mkdtemp(prefix="jobo-email-copies-"))
os.environ.pop("JOBO_EMAIL_APPLY", None)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module3 import claude_client  # noqa: E402
from module4 import agent_driver, blockers, budget_guard, email_apply, prompts, query, runner  # noqa: E402
from policy import personal  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, bool(ok), note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:64s} {note if not ok else ''}")


# Verbatim from the 2026-10-17 DawerOne 1844 session (truncated body).
DAWER_HREF = (
    "mailto:careers@dawerone.com?subject=Application%3A%20Backend%20and%20Infrastructure%20"
    "Engineer%20%E2%80%93%20%5BYour%20name%5D&body=Hi%20DawerOne%20team%2C%0A%0AI%20would%20"
    "like%20to%20apply%20for%20this%20role.%0A%0AName%3A%0A1.%20Describe%20a%20production%20"
    "backend.%0AAnswer%3A%0A"
)
NATTON_STUCK = (
    'Natton.ai posting has no web application form, only "Apply via email" '
    "(mailto:careers@natton.ai) requiring sending an email with CV attached, which this "
    "browser-based agent cannot do"
)
DAWER_STUCK = (
    "Job posting has no web application form, only a mailto: link requiring manual email "
    "composition with resume attachment — no mechanism exists to submit via browser automation"
)


class FakeGmail:
    def __init__(self, address: str):
        self.address = address
        self.drafts_made: list[bytes] = []
        self.sent: list[bytes] = []
        self.drafts_sent: list[str] = []

    def users(self):
        return self

    def drafts(self):
        return SimpleNamespace(create=self._create, send=self._send_draft)

    def messages(self):
        return SimpleNamespace(send=self._send)

    def getProfile(self, userId):
        return SimpleNamespace(execute=lambda: {"emailAddress": self.address})

    def _create(self, userId, body):
        import base64

        self.drafts_made.append(base64.urlsafe_b64decode(body["message"]["raw"]))
        return SimpleNamespace(execute=lambda: {"id": f"r-draft{len(self.drafts_made)}"})

    def _send(self, userId, body):
        import base64

        self.sent.append(base64.urlsafe_b64decode(body["raw"]))
        return SimpleNamespace(execute=lambda: {"id": f"msg{len(self.sent)}"})

    def _send_draft(self, userId, body):
        self.drafts_sent.append(body["id"])
        return SimpleNamespace(execute=lambda: {"id": f"msg-of-{body['id']}"})


def _good_body(**_k):
    name = personal.full_name()
    return email_apply.EmailDraft(
        subject=f"Application: Backend and Infrastructure Engineer – {name}",
        body=f"Hi DawerOne team,\n\nI would like to apply.\n\n1. Answer: a Go service.\n\nThanks,\n{name}",
    )


def _pdf(name: str) -> str:
    p = Path(tempfile.mkdtemp()) / name
    p.write_bytes(b"%PDF-1.4\n% test\n")
    return str(p)


def test_parse():
    print("\n[1] mailto parsing")
    m = email_apply.parse_mailto(DAWER_HREF)
    check("DawerOne recipient", m and m.to == "careers@dawerone.com", str(m))
    check("subject decoded", m and m.subject == "Application: Backend and Infrastructure Engineer – [Your name]")
    check("body template decoded", m and m.body.startswith("Hi DawerOne team,\n\nI would like") and "Answer:" in m.body)
    m = email_apply.parse_mailto(NATTON_STUCK)
    check("mailto inside agent prose", m and m.to == "careers@natton.ai" and not m.subject, str(m))
    m = email_apply.parse_mailto("Send your CV to jobs@example.org today.")
    check("bare address fallback", m and m.to == "jobs@example.org", str(m))
    check("no address -> None", email_apply.parse_mailto(DAWER_STUCK) is None)


def test_verdict():
    print("\n[2] agent verdict")
    o = agent_driver.parse_verdict(
        "STATUS: email_apply\nDETAIL: posting has no web form, only an email application to "
        f"careers@dawerone.com\nEMAIL_APPLY: {DAWER_HREF}\nFIELDS_FILLED_JSON: {{}}\n")
    check("email_apply is a valid status", o.status == "email_apply", o.status)
    check("full href kept for the subject/body", o.email_mailto == DAWER_HREF)
    o = agent_driver.parse_verdict(f"STATUS: stuck\nDETAIL: {NATTON_STUCK}\nFIELDS_FILLED_JSON: {{}}\n")
    check("old-prompt 'stuck' with an address -> email_apply (Natton 1684)",
          o.status == "email_apply" and email_apply.parse_mailto(o.email_mailto).to == "careers@natton.ai", o.status)
    o = agent_driver.parse_verdict(f"STATUS: stuck\nDETAIL: {DAWER_STUCK}\nFIELDS_FILLED_JSON: {{}}\n")
    check("no-form 'stuck' without an address stays stuck (DawerOne 1844 text)", o.status == "stuck", o.status)
    o = agent_driver.parse_verdict("STATUS: email_apply\nDETAIL: apply by email\nFIELDS_FILLED_JSON: {}\n")
    check("email_apply with no address -> stuck", o.status == "stuck" and "without a mailto" in o.detail, o.detail)
    o = agent_driver.parse_verdict("STATUS: stuck\nDETAIL: verification email never arrived from x@y.com\n")
    check("unrelated stuck naming an address stays stuck", o.status == "stuck", o.status)
    text = prompts.SYSTEM_PROMPT if hasattr(prompts, "SYSTEM_PROMPT") else Path(prompts.__file__).read_text()
    check("prompt teaches STATUS: email_apply + EMAIL_APPLY line",
          "STATUS: email_apply" in text and "EMAIL_APPLY:" in text)


def test_compose_and_send():
    print("\n[3] compose, attach, draft, send")
    job = SimpleNamespace(id=1844, company_name="DawerOne", job_title="Backend and Infrastructure Engineer",
                          location="Gurgaon", job_url="https://www.linkedin.com/jobs/view/1/", metadata_json="{}",
                          module3_fit_reasoning="fits", module3_resume_pdf_path=_pdf("resume.pdf"),
                          module3_cover_pdf_path=_pdf("cover.pdf"))
    mailto = email_apply.parse_mailto(DAWER_HREF)
    seen = {}

    def gen(**k):
        seen.update(k)
        return _good_body()

    svc = FakeGmail(personal.email())
    res = email_apply.apply_by_email(job, mailto, how="draft", generate=gen, svc=svc)
    check("prompt carries the posting's subject and template",
          "[Your name]" in seen["user_text"] and "Answer:" in seen["user_text"])
    check("prompt carries the candidate facts", personal.candidate_facts()[:40] in seen["system_instruction"])
    check("draft mode: a draft, nothing sent", len(svc.drafts_made) == 1 and not svc.sent and not res.sent)
    check("draft status names the draft and recipient",
          res.status.startswith("email_draft:draft_id=r-draft1 to=careers@dawerone.com |"), res.status)
    check("parked_draft reads it back", email_apply.parked_draft(res.status) == ("r-draft1", "careers@dawerone.com"))
    msg = message_from_bytes(svc.drafts_made[0], policy=email_policy.default)
    names = [p.get_filename() for p in msg.iter_attachments()]
    check("To / From / Subject", msg["To"] == "careers@dawerone.com" and personal.email() in msg["From"]
          and msg["Subject"].endswith(personal.full_name()), f"{msg['To']} {msg['From']} {msg['Subject']}")
    check("resume + cover attached under the job's upload names",
          len(names) == 2 and "_Resume_1844_" in names[0] and "_Cover_Letter_1844_" in names[1], str(names))
    check("plain-text body", msg.get_body(("plain",)).get_content().strip() == res.body)

    res = email_apply.apply_by_email(job, mailto, how="send", generate=gen, svc=svc)
    check("send mode: sent, status has the gmail id",
          res.sent and len(svc.sent) == 1 and res.status == "submitted:email gmail_id=msg1 to=careers@dawerone.com",
          res.status)

    def raises(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            return str(e)
        return ""

    bad = email_apply.EmailDraft(subject="Application – [Your name]", body=f"Hi\n{personal.full_name()}")
    check("unfilled [Your name] refused", "placeholder" in raises(
        lambda: email_apply.apply_by_email(job, mailto, how="send", generate=lambda **k: bad, svc=svc)))
    bad = email_apply.EmailDraft(subject="Application", body=f"1. Q?\nAnswer:\n\n{personal.full_name()}")
    check("blank Answer: line refused", "placeholder" in raises(
        lambda: email_apply.apply_by_email(job, mailto, how="send", generate=lambda **k: bad, svc=svc)))
    check("other mailbox's token refused", "not the candidate" in raises(
        lambda: email_apply.apply_by_email(job, mailto, how="send", generate=gen, svc=FakeGmail("x@other.com"))))
    nores = SimpleNamespace(**{**vars(job), "module3_resume_pdf_path": "/nonexistent.pdf"})
    check("no tailored resume refused", "no tailored resume" in raises(
        lambda: email_apply.apply_by_email(nores, mailto, how="send", generate=gen, svc=svc)))
    check("nothing else went out", len(svc.sent) == 1)


def _session():
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(s, jid, *, attempts=0, last=None):
    j = JobModel(id=jid, company_name="Natton AI", job_title="Agent Platform Engineer",
                 job_url=f"https://www.linkedin.com/jobs/view/{jid}/", should_apply=True, applied=False,
                 module2_enriched_at=datetime.datetime.now(), module3_resume_pdf_path=_pdf("resume.pdf"),
                 module4_attempts=attempts, module4_last_status=last)
    s.add(j)
    s.commit()
    return j


def _run(s, outcome, svc, how):
    spawned = []

    def fake_agent(**k):
        spawned.append(k["job_id"])
        return outcome

    with ExitStack() as st:
        st.enter_context(patch.dict(os.environ, {"JOBO_EMAIL_APPLY": how}))
        st.enter_context(patch.object(email_apply, "_service", return_value=svc))
        st.enter_context(patch.object(claude_client, "generate_structured", side_effect=lambda **k: _good_body()))
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
        res = runner.run_module4(s, budget_check=lambda: budget_guard.decide(None, 1.0),
                                 attach_check=lambda *a: agent_driver.AttachCheck(True, "test"))
    return res, spawned


def test_runner():
    print("\n[4] runner")
    outcome = agent_driver.parse_verdict(
        "STATUS: email_apply\nDETAIL: only an email application\n"
        "EMAIL_APPLY: mailto:careers@natton.ai\nFIELDS_FILLED_JSON: {}\n")

    s, svc = _session(), FakeGmail(personal.email())
    j = _job(s, 1684)
    res, spawned = _run(s, outcome, svc, "send")
    s.refresh(j)
    check("send: counted as submitted and emailed", res.submitted == 1 and res.emailed == 1 and res.failed == 0)
    check("send: applied, status keeps the gmail id",
          j.applied and j.module4_last_status == "submitted:email gmail_id=msg1 to=careers@natton.ai"
          and j.module4_outcome == "submitted", j.module4_last_status)
    check("send: no attempt charged", (j.module4_attempts or 0) == 0)

    s, svc = _session(), FakeGmail(personal.email())
    j = _job(s, 1684)
    res, spawned = _run(s, outcome, svc, "draft")
    s.refresh(j)
    check("draft: drafted, nothing sent, not applied",
          res.email_drafted == 1 and not svc.sent and not j.applied and res.submitted == 0)
    check("draft: parked, still queued, no attempt",
          email_apply.is_parked(j.module4_last_status) and j.should_apply and (j.module4_attempts or 0) == 0,
          j.module4_last_status)
    res, spawned = _run(s, outcome, svc, "draft")
    check("draft mode again: no agent, no second draft", spawned == [] and len(svc.drafts_made) == 1)
    check("--job-id still reaches a parked draft", [r.id for r in query.list_jobs_to_apply(s, job_id=1684)] == [1684])
    res, spawned = _run(s, outcome, svc, "send")
    s.refresh(j)
    check("send mode: the parked draft is sent as is, no agent",
          spawned == [] and svc.drafts_sent == ["r-draft1"] and not svc.sent and res.emailed == 1, str(spawned))
    check("send mode: applied with the sent draft's message id",
          j.applied and j.module4_last_status == "submitted:email gmail_id=msg-of-r-draft1 to=careers@natton.ai",
          j.module4_last_status)

    s, svc = _session(), FakeGmail("someone@else.com")
    j = _job(s, 1684)
    res, _ = _run(s, outcome, svc, "send")
    s.refresh(j)
    check("Gmail refusal: an ordinary failed attempt, nothing sent",
          res.failed == 1 and not j.applied and j.module4_attempts == 1 and not svc.sent, j.module4_last_status)

    print("\n[5] requeued no-web-form jobs are retried, not dropped")
    last = f"stuck:{NATTON_STUCK}"
    with patch.dict(os.environ, {"JOBO_EMAIL_APPLY": "send"}):
        check("email apply on: not a permanent blocker", blockers.permanent_reason_from_last_status(last) is None)
    with patch.dict(os.environ, {"JOBO_EMAIL_APPLY": "off"}):
        check("email apply off: still dropped as before",
              blockers.permanent_reason_from_last_status(last) == "no web application form")
    s, svc = _session(), FakeGmail(personal.email())
    j = _job(s, 1684, attempts=1, last=last)
    res, spawned = _run(s, outcome, svc, "send")
    s.refresh(j)
    check("requeued 1684 reaches the agent and is emailed", spawned == [1684] and j.applied, str(spawned))


def main() -> int:
    test_parse()
    test_verdict()
    test_compose_and_send()
    test_runner()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
