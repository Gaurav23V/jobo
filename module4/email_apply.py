"""Apply by email when a posting has no web form, only a mailto link (GHO-67).

Natton AI 1684 and DawerOne 1844 (Oct 2026) say "send your CV to
careers@..." and nothing else. The browser agent cannot send mail, so both
ended as `stuck` and were dropped. Now the agent reports `STATUS: email_apply`
with the posting's mailto link, and this module does the rest in code:

  1. parse the link: recipient, the subject the posting asks for, and any
     body template it pre-fills (DawerOne's has four questions to answer);
  2. write a short plain-text body from personal/candidate_facts.md, the
     profile and the job (one `claude -p` call, module3/claude_client.py);
  3. attach the job's tailored resume, and its cover letter if there is one;
  4. send it from the candidate's Gmail with the collector's OAuth token
     (scope gmail.modify, which can send) - or, in draft mode, only create a
     Gmail draft for the candidate to read first.

The agent never touches mail: like the OTP lookup, the driver is the gate.

MODE (JOBO_EMAIL_APPLY overrides): "send" sends and counts the job as
submitted; "draft" creates a draft and parks the job as `email_draft:` until
the mode is "send", when the runner sends that same draft without spawning an
agent; "off" leaves the job stuck as before.
"""

from __future__ import annotations

import base64
import datetime
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from pydantic import BaseModel, Field

from module4 import upload_names
from policy import personal

logger = logging.getLogger(__name__)

# The candidate has not yet seen a first email (G, GHO-67): drafts only.
MODE = "draft"
MODES = ("send", "draft", "off")

STATUS = "email_apply"
DRAFT_PREFIX = "email_draft:"

_ADDR = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_MAILTO_RE = re.compile(r"mailto:[^\s\"'<>`)\]]+", re.I)
_ADDR_RE = re.compile(_ADDR)
# How an agent describes a posting whose only apply path is an email
# (module4/blockers.py, module4/run_report.py: signature no_web_form).
_NO_WEB_FORM_RE = re.compile(
    r"mailto:|no web (?:application )?form|no (?:online )?application form|"
    r"apply (?:via|by) email|email only", re.I)
_DRAFT_ID_RE = re.compile(r"draft_id=(\S+)")
# Left-over template slots ("[Your name]", "<name>") or an unanswered
# "Answer:" line: the body is not finished and must not go out.
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,40}\]|<[A-Za-z ]{2,30}>|^\s*Answer:\s*$", re.M)
MAX_BODY_CHARS = 6000


def mode() -> str:
    m = os.environ.get("JOBO_EMAIL_APPLY", "").strip().lower() or MODE
    return m if m in MODES else "off"


def no_web_form(text: str | None) -> bool:
    return bool(_NO_WEB_FORM_RE.search(text or ""))


@dataclass
class Mailto:
    to: str
    subject: str = ""
    body: str = ""


def parse_mailto(text: str | None) -> Mailto | None:
    """The first mailto link in `text`, or a bare address as a fallback."""
    text = text or ""
    m = _MAILTO_RE.search(text)
    if m:
        href = m.group(0).rstrip(".,;")
        parts = urllib.parse.urlsplit(href)
        to = urllib.parse.unquote(parts.path).split(",")[0].strip()
        q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        q = {k.lower(): v for k, v in q.items()}
        if _ADDR_RE.fullmatch(to):
            return Mailto(
                to=to,
                subject=(q.get("subject") or [""])[0].strip(),
                body=(q.get("body") or [""])[0].replace("\r\n", "\n").strip(),
            )
    m = _ADDR_RE.search(text)
    return Mailto(to=m.group(0)) if m else None


class EmailDraft(BaseModel):
    subject: str = Field(description="Subject line, plain text, no placeholders.")
    body: str = Field(description="Plain-text email body, signed with the candidate's name.")


def _system_prompt() -> str:
    return f"""You write one short job application email for the candidate below,
to send from their own mailbox with their resume attached. Plain text only.

Rules:
- Use only facts from the candidate facts and profile. Never invent an
  employer, project, skill, number or credential. If the posting asks
  something the facts do not cover, answer honestly with the closest real
  experience, or say plainly that the candidate has not done it.
- If the posting supplies a body template, keep its structure and fill
  every field and answer every question; drop its instructions to the
  applicant (e.g. "[Please attach ...]"). Leave no placeholder or blank
  "Answer:" line. Keep each answer to 2-4 sentences.
- Without a template: 90-160 words. Who the candidate is, why this role,
  one or two concrete matching things they built, and that the resume is
  attached. No flattery, no buzzwords, no bullet lists.
- Subject: the subject the posting asks for, with its placeholders filled
  (e.g. "[Your name]" -> the candidate's name). If none, use
  "Application: <job title> - <candidate name>".
- End with the candidate's name, phone and LinkedIn URL on separate lines.

Candidate facts:
{personal.candidate_facts()}

Candidate profile:
{personal.profile_path().read_text(encoding="utf-8") if personal.profile_path().is_file() else ""}
"""


def _job_text(job, mailto: Mailto, note: str) -> str:
    return (
        f"Today: {datetime.date.today().isoformat()}\n"
        f"Company: {job.company_name}\nJob title: {job.job_title}\n"
        f"Location: {job.location or ''}\nPosting: {job.job_url}\n"
        f"Job details (JSON): {(job.metadata_json or '')[:6000]}\n"
        f"Why the candidate fits (triage notes): {(job.module3_fit_reasoning or '')[:1500]}\n\n"
        f"How the posting asks to apply: {note[:600]}\n"
        f"Send to: {mailto.to}\n"
        f"Subject the posting asks for: {mailto.subject or '(none)'}\n"
        f"Body template from the posting:\n{mailto.body or '(none)'}\n"
    )


def compose(job, mailto: Mailto, note: str = "", *, generate=None) -> EmailDraft:
    """Subject + body for this job. Raises if the result is not sendable."""
    if generate is None:
        from module3 import claude_client

        generate = claude_client.generate_structured
    draft = generate(
        system_instruction=_system_prompt(),
        user_text=_job_text(job, mailto, note),
        response_model=EmailDraft,
    )
    draft.subject = " ".join(draft.subject.split())
    draft.body = draft.body.strip()
    problem = check_draft(draft)
    if problem:
        raise ValueError(f"email body not sendable: {problem}")
    return draft


def check_draft(draft: EmailDraft) -> str:
    if not draft.subject or not draft.body:
        return "empty subject or body"
    if len(draft.body) > MAX_BODY_CHARS:
        return f"body is {len(draft.body)} chars"
    for text in (draft.subject, draft.body):
        m = _PLACEHOLDER_RE.search(text)
        if m:
            return f"unfilled placeholder {m.group(0).strip()!r}"
    if personal.full_name() and personal.full_name() not in draft.body:
        return "body is not signed with the candidate's name"
    return ""


def attachments(job) -> list[Path]:
    """Upload-named copies of the tailored resume (required) and cover letter."""
    out = []
    for kind, src in (("resume", job.module3_resume_pdf_path), ("cover", job.module3_cover_pdf_path)):
        if src and Path(src).is_file():
            out.append(Path(upload_names.prepare(job.id, src, kind) or src))
        elif kind == "resume":
            raise FileNotFoundError(f"job {job.id} has no tailored resume at {src!r}")
    return out


def build_message(to: str, draft: EmailDraft, files: list[Path]) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = f"{personal.full_name()} <{personal.email()}>"
    msg["Subject"] = draft.subject
    msg.set_content(draft.body)
    for f in files:
        msg.add_attachment(f.read_bytes(), maintype="application", subtype="pdf", filename=f.name)
    return msg


def _service():
    from module4 import otp_lookup

    return otp_lookup._gmail_service()


def _check_sender(svc) -> None:
    """The token's mailbox must be the candidate's: the From line says so."""
    addr = svc.users().getProfile(userId="me").execute().get("emailAddress", "")
    if addr.lower() != personal.email().lower():
        raise RuntimeError(f"Gmail token is for {addr!r}, not the candidate's {personal.email()!r}")


def create_draft(msg: EmailMessage, svc=None) -> str:
    svc = svc or _service()
    _check_sender(svc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    d = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return d["id"]


def send_message(msg: EmailMessage, svc=None) -> str:
    svc = svc or _service()
    _check_sender(svc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return svc.users().messages().send(userId="me", body={"raw": raw}).execute()["id"]


def send_draft(draft_id: str, svc=None) -> str:
    svc = svc or _service()
    _check_sender(svc)
    return svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()["id"]


def draft_status(draft_id: str, to: str, subject: str) -> str:
    return f"{DRAFT_PREFIX}draft_id={draft_id} to={to} | {subject[:200]}"


def sent_status(message_id: str, to: str) -> str:
    return f"submitted:email gmail_id={message_id} to={to}"


def is_parked(last_status: str | None) -> bool:
    return (last_status or "").startswith(DRAFT_PREFIX)


def parked_draft(last_status: str | None) -> tuple[str, str] | None:
    """(draft id, recipient) of a parked `email_draft:` status."""
    if not is_parked(last_status):
        return None
    m = _DRAFT_ID_RE.search(last_status or "")
    to = _ADDR_RE.search((last_status or "").split(" | ", 1)[0])
    return (m.group(1), to.group(0) if to else "") if m else None


@dataclass
class EmailResult:
    status: str  # the module4_last_status to store
    sent: bool
    message_id: str = ""
    draft_id: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    files: tuple[str, ...] = ()


def apply_by_email(job, mailto: Mailto, note: str = "", *, how: str | None = None,
                   generate=None, svc=None) -> EmailResult:
    """Write the email for `job` and send it (or draft it, in draft mode)."""
    how = how or mode()
    if how not in ("send", "draft"):
        raise ValueError(f"email apply is {how}")
    draft = compose(job, mailto, note, generate=generate)
    files = attachments(job)
    msg = build_message(mailto.to, draft, files)
    common = dict(to=mailto.to, subject=draft.subject, body=draft.body,
                  files=tuple(f.name for f in files))
    if how == "draft":
        did = create_draft(msg, svc)
        logger.info("[%s] Email application drafted (draft %s) to %s.", job.id, did, mailto.to)
        return EmailResult(status=draft_status(did, mailto.to, draft.subject), sent=False,
                           draft_id=did, **common)
    mid = send_message(msg, svc)
    logger.info("[%s] Email application sent (gmail id %s) to %s.", job.id, mid, mailto.to)
    return EmailResult(status=sent_status(mid, mailto.to), sent=True, message_id=mid, **common)
