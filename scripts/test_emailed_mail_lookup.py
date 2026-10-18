"""Offline replay of two "the site says it mailed you, the inbox has nothing"
misses from run_20261018_2200.log (GHO-58).

Job 1899 (TalentRecruit OTP). The form lived at <tenant>.talentrecruit.com and
the agent asked `NEED_OTP: <tenant>.talentrecruit.com`, then
`NEED_OTP: talentrecruit.com`. The code arrived five times, in the inbox, as
    From: QXR India Recruiting <recruiting@qxr.com>
    Subject: Candidate Verification Email
and every poll skipped it ("neither sender headers nor body show the requested
site"): TalentRecruit was not a known white-label ATS, its tenant was not read,
and the employer phrase "qxr innovation centre" never appears whole in the
sender. The fix: TalentRecruit is a white-label vendor, its tenant is read
from the host, and the company's leading word counts once the tenant spells
the name's first words ("qxrinnovation").

Job 1887 (SAP SuccessFactors reset). Three "Password Reset" mails arrived from
system@successfactors.com, the reset URL only in <a href='...'>Click Here</a>
(HTML inside the text/plain part). Stripping tags dropped it, so the lookup
reported "no password-reset email for sapsf.com" and the job was dropped.

Names, digits and tokens are fictional; the shapes are the real mails'.
No network, no Gmail, no browser.

Run:  .venv/bin/python scripts/test_emailed_mail_lookup.py
"""

from __future__ import annotations

import base64
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from module4 import otp_lookup, password_reset as pr  # noqa: E402

# --- fixtures -------------------------------------------------------------------

COMPANY = "QXR Innovation Centre India"
TENANT_HOST = "qxrinnovation.talentrecruit.com"
FORM_URL = f"https://{TENANT_HOST}/career-page/apply/U2FsdGVkX1fake"


def qxr_mail(code: str) -> dict:
    return {
        "from": "QXR India Recruiting <recruiting@qxr.com>",
        "subject": "Candidate Verification Email",
        "headers": {"Return-Path": "<prvs=7442fake9=recruiting@qxr.com>"},
        "body": (
            "View Email in Browser [https://talentimages.s3.ap-south-1.amazonaws.com/"
            "verification-code.png] THANKS FOR APPLYING TO A JOB ! Verify Your E-mail "
            "Address Hi Alex, You're almost there to apply for a job Software Engineer. "
            "Please use the below verification code to verify your email address. "
            f"{code} Best regards, Talent Acquisition Team QXR Innovation Centre India\n"
        ),
    }


LINKEDIN_SENT_TO_QXR = {
    "from": "LinkedIn <jobs-noreply@linkedin.com>",
    "subject": "Alex, your application was sent to QXR Innovation Centre India",
    "body": "Your application was sent to QXR. Verification code 551201\n",
}

# SuccessFactors reset mail: HTML in a text/plain part, URL only in the href.
SF_RESET_HTML = (
    "<p>Hello Alex,<br />\n<br />\nPlease click on the URL below to change your "
    "password.<br />\n<br />\n<a href='https://career41.sapsf.com/career?company="
    "hazeltonin&amp;site=VjItZmFrZVNpdGVUb2tlbg&amp;login%5fns=pwd%5freset&amp;"
    "account=ZmFrZUFjY291bnRUb2tlbg&amp;verify=ZmFrZVZlcmlmeVRva2Vu&amp;"
    "clientId=jobs2web&'>Click Here</a><br />\n<br />\n<br/>PerformanceManager"
    "<br/><br/>Copyright 2018 SuccessFactors, Inc. All rights reserved.<br/><br/>"
    "This message was sent by the PerformanceManager application.</p>"
)
SF_RESET = {
    "from": "Hazelton Talent Acquisition <system@successfactors.com>",
    "subject": "Password Reset",
    "body": SF_RESET_HTML,
}


# --- fake Gmail ---------------------------------------------------------------------


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeGmail:
    def __init__(self):
        self.msgs: list[dict] = []

    def add(self, mail: dict, *, age_sec: float, mime: str = "text/plain") -> None:
        received = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        headers = [
            {"name": "From", "value": mail["from"]},
            {"name": "Subject", "value": mail["subject"]},
        ]
        headers += [{"name": k, "value": v} for k, v in (mail.get("headers") or {}).items()]
        self.msgs.append(
            {
                "id": f"m{len(self.msgs)}",
                "internalDate": str(int(received.timestamp() * 1000)),
                "payload": {
                    "mimeType": mime,
                    "headers": headers,
                    "body": {"data": base64.urlsafe_b64encode(mail["body"].encode()).decode()},
                },
            }
        )

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q, maxResults, **_kw):
        hits = [m for m in self.msgs if self._match(m, q)]
        hits.sort(key=lambda m: int(m["internalDate"]), reverse=True)
        return _Exec({"messages": [{"id": m["id"]} for m in hits[:maxResults]]})

    def get(self, *, userId, id, format, **_kw):
        return _Exec(next(m for m in self.msgs if m["id"] == id))

    @staticmethod
    def _match(m: dict, q: str) -> bool:
        hdr = {h["name"].lower(): h["value"].lower() for h in m["payload"]["headers"]}
        for f in re.findall(r"from:(\S+)", q):
            if f.startswith("("):
                continue
            if f.lower() not in hdr.get("from", ""):
                return False
        for s in re.findall(r'subject:"([^"]*)"', q):
            if s.lower() not in hdr.get("subject", ""):
                return False
        return True


def code_for(inbox: FakeGmail, sender: str, hint: str = "", company: str = COMPANY, urls=()):
    with patch.object(otp_lookup, "_gmail_service", lambda: inbox):
        return otp_lookup.find_code(
            sender=sender, minutes=10, subject_hint=hint, company=company, employer_urls=tuple(urls)
        ).code


def reset_for(inbox: FakeGmail, domain: str):
    with patch.object(otp_lookup, "_gmail_service", lambda: inbox):
        return pr.find_reset_link(domain=domain, minutes=15)


# --- cases --------------------------------------------------------------------------


def otp_cases() -> list[tuple[str, object, object]]:
    out = []

    # The first request of the run: the agent named the tenant host.
    g = FakeGmail()
    g.add(qxr_mail("381547"), age_sec=30)
    out.append(("talentrecruit_tenant_request_finds_code", "381547",
                code_for(g, TENANT_HOST, "Please check your email")))

    # Later requests named only the vendor; the tenant comes from the open tab.
    out.append(("vendor_request_with_open_tab_finds_code", "381547",
                code_for(g, "talentrecruit.com", "", urls=(FORM_URL,))))

    # Resends: the newest code is the live one.
    g = FakeGmail()
    g.add(qxr_mail("381547"), age_sec=200)
    g.add(qxr_mail("640913"), age_sec=20)
    out.append(("resend_newest_code_wins", "640913", code_for(g, TENANT_HOST)))

    # Fail safe: nothing ties qxr.com to this job without the tenant.
    g = FakeGmail()
    g.add(qxr_mail("381547"), age_sec=30)
    out.append(("vendor_request_without_tenant_refused", None, code_for(g, "talentrecruit.com")))

    # Another employer's TalentRecruit tenant does not vouch for QXR mail.
    out.append(("other_tenant_refused", None,
                code_for(g, "harborpoint.talentrecruit.com")))

    # Same mail on someone else's job.
    out.append(("other_company_refused", None,
                code_for(g, TENANT_HOST, company="Harbor Point Analytics")))

    # A non-vendor site named: the employer's code belongs to another form.
    out.append(("non_vendor_hint_refused", None, code_for(g, "jobrelay.com", company=COMPANY)))

    # LinkedIn receipt naming the employer, with a code-shaped number.
    g = FakeGmail()
    g.add(LINKEDIN_SENT_TO_QXR, age_sec=10)
    out.append(("linkedin_receipt_refused", None, code_for(g, TENANT_HOST)))
    return out


def unit_cases() -> list[tuple[str, object, object]]:
    lead = otp_lookup.tenant_confirmed_lead
    tenants = otp_lookup.employer_tenants
    return [
        ("talentrecruit_tenant_read", ("qxrinnovation",), tenants([FORM_URL], {"talentrecruit"})),
        ("talentrecruit_tenant_needs_its_family", (), tenants([FORM_URL], {"workday"})),
        ("talentrecruit_is_white_label", True,
         otp_lookup.SiteHint.parse("talentrecruit.com").vendor_named),
        ("lead_confirmed_by_two_word_tenant", ("qxr",), lead(COMPANY, ["qxrinnovation"])),
        ("lead_confirmed_by_full_tenant", ("qxr",), lead(COMPANY, ["qxrinnovationcentre"])),
        ("lead_not_confirmed_by_lead_only_tenant", (), lead("Harbor Point", ["harbor"])),
        ("lead_not_confirmed_by_unrelated_tenant", (), lead(COMPANY, ["harborpoint"])),
        ("lead_needs_three_letters", (), lead("QX Innovation", ["qxinnovation"])),
        ("single_word_company_adds_nothing", (), lead("Treyco", ["treyco"])),
        ("vendor_word_never_a_lead", (), lead("Lever Data Works", ["leverdata"])),
        ("no_tenant_no_lead", (), lead(COMPANY, [])),
    ]


def reset_cases() -> list[tuple[str, object, object]]:
    out = []

    def host(r):
        return pr._host_of(r.url or "")

    # The real shape: HTML inside text/plain, URL only in the href.
    g = FakeGmail()
    g.add(SF_RESET, age_sec=60)
    r = reset_for(g, "sapsf.com")
    out.append(("sf_href_only_reset_link_found", "career41.sapsf.com", host(r)))
    out.append(("sf_link_is_the_reset_action", True, "pwd%5freset" in (r.url or "")))
    out.append(("sf_href_entities_unescaped", False, "&amp;" in (r.url or "")))

    # The same mail as a text/html-only part.
    g = FakeGmail()
    g.add(SF_RESET, age_sec=60, mime="text/html")
    out.append(("sf_html_only_part_link_found", "career41.sapsf.com", host(reset_for(g, "sapsf.com"))))

    # Domain safety still holds for hrefs.
    g = FakeGmail()
    g.add(SF_RESET, age_sec=60)
    out.append(("href_for_other_domain_refused", None, reset_for(g, "totally-different.com").url))
    evil = dict(SF_RESET, body=SF_RESET_HTML.replace("career41.sapsf.com", "login.example.net"))
    g = FakeGmail()
    g.add(evil, age_sec=60)
    out.append(("unrelated_href_refused", None, reset_for(g, "sapsf.com").url))

    # Stale reset mail is still ignored.
    g = FakeGmail()
    g.add(SF_RESET, age_sec=40 * 60)
    out.append(("stale_reset_mail_ignored", None, reset_for(g, "sapsf.com").url))

    # Plain text is passed through untouched.
    plain = "Reset: https://x.example.com/reset?token=abc"
    out.append(("plain_text_unchanged", plain, pr.mail_text(plain)))
    out.append(("anchor_keeps_label", True, "Click Here" in pr.mail_text(SF_RESET_HTML)))
    return out


def main() -> int:
    failed = total = 0
    for title, rows in (
        ("TalentRecruit OTP (job 1899)", otp_cases()),
        ("tenant / lead-word units", unit_cases()),
        ("SuccessFactors reset link (job 1887)", reset_cases()),
    ):
        print(f"\n  -- {title} --")
        for label, expected, got in rows:
            ok = got == expected
            total += 1
            failed += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {label:46s} expected={expected!r} got={got!r}")
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
