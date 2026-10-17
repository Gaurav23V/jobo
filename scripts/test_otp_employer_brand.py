"""Offline replay of the Treyco / Oracle Cloud Recruiting OTP miss (job 1429).

What happened (run_20261011_1555.log 19:28-19:31, and run_20261004_1719.log):
  the agent asked `NEED_OTP: oraclecloud.com | Verify it's you`. Oracle Fusion
  mailed the code from Treyco's own domain:
      From: Treyco-Fusion-Notification <Treyco-Fusion-Notification@treycoservice.com>
      Subject: Confirm your identity
  The open pass found the mail on every poll (24 times) and skipped it:
  "neither sender headers nor body show the requested site". The body never
  says Oracle; it is signed "Treyco Talent Acquisition Team".

The fix lets the job's employer count as the requested site when the agent
named a white-label ATS vendor. This suite proves the Treyco mail now yields its
code, and that everything the sender guard protects still holds: other
employers, job boards (Naukri, Indeed, Shine, LinkedIn), the LinkedIn PIN on a
Humongo job, stale mail, non-vendor hints, and short-brand false positives.

No network, no Gmail, no browser: Gmail is a fake service (same as
scripts/test_otp_sender_guard.py).

Run:  .venv/bin/python scripts/test_otp_employer_brand.py
"""

from __future__ import annotations

import base64
import importlib.util
import inspect
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- fixtures -----------------------------------------------------------------

# The real mail (two copies, 2026-10-11 ~19:28 and ~19:30 IST), digits swapped.
Treyco_ORACLE = {
    "from": "Treyco-Fusion-Notification <Treyco-Fusion-Notification@treycoservice.com>",
    "subject": "Confirm your identity",
    "body": (
        "Hello Alex,\r\n\r\nJust one more step before you get started.\r\n\r\n"
        "You must confirm your identity using the one-time pass code : 482915\r\n\r\n"
        "Note : This code will expire in 10 minutes.\r\n\r\nSincerely,\r\n"
        "Treyco Talent Acquisition Team\r\n"
    ),
    "headers": {"Return-Path": "<Treyco-Fusion-Notification@treycoservice.com>"},
}
Treyco_ORACLE_RESEND = dict(Treyco_ORACLE, body=Treyco_ORACLE["body"].replace("482915", "730114"))

# LinkedIn receipt naming the employer: must never be a code source.
LINKEDIN_SENT_TO_Treyco = {
    "from": "LinkedIn <jobs-noreply@linkedin.com>",
    "subject": "Alex, your application was sent to Treyco",
    "body": (
        "Your application was sent to Treyco\r\nGen AI Engineer\r\nTreyco\r\nGurugram\r\n"
        "View job: https://www.linkedin.com/comm/jobs/view/4000000105/?trk=eml-x"
        "-null-qz7k2m~xp4mtrlc~w7-null-null&otpToken=FAKEOTPTOKENFAKE\r\n"
    ),
}
LINKEDIN_PIN = {
    "from": "LinkedIn <security-noreply@linkedin.com>",
    "subject": "Alex, here's your PIN 417092",
    "body": "Hi Alex,\n\nPlease use this verification code to complete your sign in: 417092\n",
}
# Job-board mails that carry a code AND name the employer.
JOB_BOARD_MAILS = [
    (
        "naukri",
        {
            "from": "Naukri <info@naukri.com>",
            "subject": "OTP for your Treyco application",
            "body": "Your one-time pass code for the Treyco application is 551201.\n",
        },
    ),
    (
        "indeed",
        {
            "from": "Treyco via Indeed <alert@indeed.com>",
            "subject": "Verify it's you",
            "body": "Your verification code is 551202. Treyco Talent Acquisition Team\n",
        },
    ),
    (
        "shine",
        {
            "from": "\"Shine.com\" <jobs@jobsalert.shine.com>",
            "subject": "Your OTP to apply at Treyco",
            "body": "Your OTP is 551203. Apply to Treyco on https://www.shine.com/jobs/treyco\n",
        },
    ),
]


# --- fake Gmail (same contract as test_otp_sender_guard.FakeGmail) -------------


class FakeGmail:
    def __init__(self):
        self.msgs: list[dict] = []
        self.queries: list[str] = []

    def add(self, mail: dict, *, age_sec: float) -> None:
        received = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        headers = [
            {"name": "From", "value": mail["from"]},
            {"name": "Subject", "value": mail["subject"]},
        ]
        for k, v in (mail.get("headers") or {}).items():
            headers.append({"name": k, "value": v})
        self.msgs.append(
            {
                "id": f"m{len(self.msgs)}",
                "internalDate": str(int(received.timestamp() * 1000)),
                "payload": {
                    "mimeType": "text/plain",
                    "headers": headers,
                    "body": {"data": base64.urlsafe_b64encode(mail["body"].encode()).decode()},
                },
            }
        )

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q, maxResults):
        self.queries.append(q)
        hits = [m for m in self.msgs if self._match(m, q)]
        hits.sort(key=lambda m: int(m["internalDate"]), reverse=True)
        return _Exec({"messages": [{"id": m["id"]} for m in hits[:maxResults]]})

    def get(self, *, userId, id, format):
        return _Exec(next(m for m in self.msgs if m["id"] == id))

    @staticmethod
    def _match(m: dict, q: str) -> bool:
        hdr = {h["name"].lower(): h["value"].lower() for h in m["payload"]["headers"]}
        for f in re.findall(r"from:(\S+)", q):
            if f.lower() not in hdr.get("from", ""):
                return False
        for s in re.findall(r'subject:"([^"]*)"', q):
            if s.lower() not in hdr.get("subject", ""):
                return False
        return True


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


def lookup(mod: ModuleType, inbox: FakeGmail, sender: str, hint: str, company: str = "", **kw):
    """find_code as the driver now calls it. An old module that does not take
    `company` answers None (it cannot know the employer), which is exactly
    the behaviour this suite asserts is wrong for the Treyco mail."""
    mod._gmail_service = lambda: inbox
    try:
        return mod.find_code(sender=sender, minutes=10, subject_hint=hint, company=company, **kw).code
    except TypeError as e:
        if "company" in str(e) or "employer_urls" in str(e):
            return mod.find_code(sender=sender, minutes=10, subject_hint=hint).code
        raise


# --- cases ------------------------------------------------------------------------

ORACLE = ("oraclecloud.com", "Verify it's you")


def mailbox_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    out = []

    # 1. The exact 2026-10-11 state: Treyco's mail is in, plus the LinkedIn
    #    "sent to Treyco" receipt (newer) as a distractor.
    g = FakeGmail()
    g.add(Treyco_ORACLE, age_sec=40)
    g.add(LINKEDIN_SENT_TO_Treyco, age_sec=5)
    out.append(("treyco_oracle_mail_accepted_for_Treyco", "482915", lookup(mod, g, *ORACLE, company="Treyco")))

    # 1b. Both copies (initial send + resend): the newest one is the live code.
    g = FakeGmail()
    g.add(Treyco_ORACLE, age_sec=150)
    g.add(Treyco_ORACLE_RESEND, age_sec=20)
    out.append(("treyco_resend_newest_code_wins", "730114", lookup(mod, g, *ORACLE, company="Treyco")))

    # 1c. The DB name with corporate noise still normalises to the brand.
    g = FakeGmail()
    g.add(Treyco_ORACLE, age_sec=40)
    out.append(
        (
            "treyco_long_legal_name_accepted",
            "482915",
            lookup(mod, g, *ORACLE, company="Treyco Service Holdings Pvt. Ltd."),
        )
    )

    # 2. Same mail, but the job is someone else's: refused.
    for other in ("Adatum", "Excel Ltd", "TT"):
        g = FakeGmail()
        g.add(Treyco_ORACLE, age_sec=40)
        out.append((f"treyco_mail_refused_for_{other.split()[0]}_job", None, lookup(mod, g, *ORACLE, company=other)))

    # 2b. No company known (old callers): unchanged, refused.
    g = FakeGmail()
    g.add(Treyco_ORACLE, age_sec=40)
    out.append(("treyco_mail_refused_without_company", None, lookup(mod, g, *ORACLE, company="")))

    # 2c. The agent named a NON-vendor site: the employer's code belongs to a
    #     different form, so it is refused even on a Treyco job.
    g = FakeGmail()
    g.add(Treyco_ORACLE, age_sec=40)
    out.append(
        ("treyco_mail_refused_when_hint_is_not_a_vendor", None, lookup(mod, g, "jobrelay.com", "", company="Treyco"))
    )

    # 2d. Stale: the Treyco mail predates this OTP request.
    g = FakeGmail()
    g.add(Treyco_ORACLE, age_sec=200)
    out.append(
        (
            "treyco_mail_before_not_before_ignored",
            None,
            lookup(mod, g, *ORACLE, company="Treyco", not_before=datetime.now(timezone.utc) - timedelta(seconds=60)),
        )
    )

    # 3. LinkedIn's sign-in PIN on a Humongo (Paradox) job: still refused, also
    #    when the agent named a vendor and the employer is known.
    for sender, hint in (("paradox.ai", "Humongo verification code"), ORACLE):
        g = FakeGmail()
        g.add(LINKEDIN_PIN, age_sec=20)
        out.append((f"linkedin_pin_refused_for_humongo[{sender}]", None, lookup(mod, g, sender, hint, company="Humongo")))

    # 4. Job-board mails naming the employer, with a code: refused.
    for label, mail in JOB_BOARD_MAILS:
        g = FakeGmail()
        g.add(mail, age_sec=20)
        out.append((f"{label}_mail_refused_for_treyco", None, lookup(mod, g, *ORACLE, company="Treyco")))

    # 5. Short-brand false positives. "TT" must not match "http" / "ttl";
    #    "Treyco" must not match "excel".
    ttl = {
        "from": "TTL Careers <otp@ttl.co.in>",
        "subject": "Confirm your identity",
        "body": "Your one-time pass code is 661204. See http://ttl.co.in/help\n",
    }
    g = FakeGmail()
    g.add(ttl, age_sec=20)
    out.append(("tt_does_not_match_ttl_or_http", None, lookup(mod, g, *ORACLE, company="TT")))
    excel = {
        "from": "Excel Recruiting <talent@excel-hr.com>",
        "subject": "Confirm your identity",
        "body": "Your one-time pass code is 661205. Excel Talent Acquisition Team\n",
    }
    g = FakeGmail()
    g.add(excel, age_sec=20)
    out.append(("treyco_does_not_match_excel", None, lookup(mod, g, *ORACLE, company="Treyco")))
    # Brand as a plain word in body prose is not evidence (company names are
    # often ordinary words: Canvas, Anchor, Compass).
    prose = {
        "from": "Accounts <no-reply@randomshop.com>",
        "subject": "Confirm your identity",
        "body": "Your one-time pass code is 661206. Our new canvas collection is here.\n",
    }
    g = FakeGmail()
    g.add(prose, age_sec=20)
    out.append(("brand_word_in_body_prose_not_enough", None, lookup(mod, g, *ORACLE, company="Canvas")))

    # 6. Positive controls for the same matcher.
    tt_real = {
        "from": "TT Talent <careers@tt.com>",
        "subject": "Confirm your identity",
        "body": "Your one-time pass code is 661207.\n",
    }
    g = FakeGmail()
    g.add(tt_real, age_sec=20)
    out.append(("tt_own_domain_accepted", "661207", lookup(mod, g, *ORACLE, company="TT")))
    relayed = {
        "from": "Careers <bounce-1@em123.sendgrid.net>",
        "subject": "Confirm your identity",
        "body": "Your one-time pass code is 661208.\nhttps://careers.adatum.com/candidate\n",
    }
    g = FakeGmail()
    g.add(relayed, age_sec=20)
    # Body links are NOT sender evidence (real inbox: newsletters link
    # apps.apple.com, examplegroup.com, *.ibm.com). Fails safe: no code.
    out.append(("employer_link_in_body_alone_not_enough", None, lookup(mod, g, "icims.com", "", company="Adatum")))
    # Sender evidence: brand in the display name of a vendor-domain mail
    # (SuccessFactors style), and subdomain-only lookalikes refused.
    sf = {
        "from": "\"Adatum Talent Acquisition\" <donotreply@successfactors.eu>",
        "subject": "Confirm your identity",
        "body": "Your one-time pass code is 661210.\n",
    }
    g = FakeGmail()
    g.add(sf, age_sec=20)
    out.append(("brand_in_display_name_accepted", "661210", lookup(mod, g, "oraclecloud.com", "", company="Adatum")))
    for label, frm, company in (
        ("mailgun_subdomain_mg_is_not_MandG", "Louie <bounce@mg.substack.com>", "R&B Example"),
        ("codeharbor_subdomain_on_devpost_refused", "Devpost <hi@codeharbor-transcend.devpost.com>", "CodeHarbor"),
    ):
        g = FakeGmail()
        g.add({"from": frm, "subject": "Confirm your identity", "body": "Your one-time pass code is 661211.\n"}, age_sec=20)
        out.append((label, None, lookup(mod, g, *ORACLE, company=company)))
    # Tenant from the ATS URL when the DB name differs from the mail brand
    # ("Poplar Consultancy Services" on LinkedIn, "pcs" on Workday and in mail).
    tenant_mail = {
        "from": "Talent Team <talent@pcs.com>",
        "subject": "Your one-time code",
        "body": "Hello,\n\nYour one-time pass code is 661209.\n",
    }
    g = FakeGmail()
    g.add(tenant_mail, age_sec=20)
    mod._gmail_service = lambda: g
    try:
        got = mod.find_code(
            sender="myworkdayjobs.com",
            minutes=10,
            subject_hint="",
            company="Poplar Consultancy Services",
            employer_urls=("https://pcs.wd3.myworkdayjobs.com/en-US/careers/job/123",),
        ).code
    except TypeError:
        got = mod.find_code(sender="myworkdayjobs.com", minutes=10, subject_hint="").code
    out.append(("workday_tenant_from_url_accepted", "661209", got))
    # A tenant read from ANOTHER vendor's page (a leftover Workday tab while
    # the agent asks about iCIMS) does not vouch for the mail.
    g = FakeGmail()
    g.add(tenant_mail, age_sec=20)
    mod._gmail_service = lambda: g
    try:
        got = mod.find_code(
            sender="icims.com",
            minutes=10,
            subject_hint="",
            company="Poplar Consultancy Services",
            employer_urls=("https://pcs.wd3.myworkdayjobs.com/en-US/careers/job/123",),
        ).code
    except TypeError:
        got = mod.find_code(sender="icims.com", minutes=10, subject_hint="").code
    out.append(("tenant_from_other_vendor_page_ignored", None, got))
    # Same mail without the tenant URL: the brand alone does not show in it.
    g = FakeGmail()
    g.add(tenant_mail, age_sec=20)
    out.append(
        ("same_mail_refused_without_tenant_url", None, lookup(mod, g, "myworkdayjobs.com", "", company="Poplar Consultancy Services"))
    )

    return out


def unit_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    out = []
    eb = getattr(mod, "employer_brands", None)
    et = getattr(mod, "employer_tenants", None)
    for company, expected in (
        ("Treyco", ("treyco",)),
        ("Treyco Service Holdings Pvt Ltd", ("treyco",)),
        ("Northwind in India", ("northwind",)),
        ("Brian's India", ("brians",)),
        ("Harbor Point", ("harbor point",)),
        ("Global Services Ltd", ()),
        ("Naukri", ()),
        ("LinkedIn", ()),
        ("Workday", ()),
    ):
        out.append((f"employer_brands[{company}]", expected, eb(company) if eb else "missing"))
    out.append(
        (
            "employer_tenants[wd1/icims/linkedin]",
            ("treyco", "adatum"),
            et(
                [
                    "https://treyco.wd1.myworkdayjobs.com/x",
                    "https://careers-adatum.icims.com/jobs/1",
                    "https://www.linkedin.com/jobs/view/1/",
                ]
            )
            if et
            else "missing",
        )
    )
    return out


def driver_cases() -> list[tuple[str, object, object]]:
    """The job's company really reaches the lookup from the apply driver."""
    out = []
    from module4 import agent_driver, otp_lookup, runner

    seen: list[dict] = []

    def fake_wait(**kw):
        seen.append(kw)
        return otp_lookup.OtpResult(code=None, error="no verification code found in the time window")

    rounds = iter(
        [
            "NEED_OTP: oraclecloud.com | Verify it's you\n\nsession_id: s1\n",
            "STATUS: stuck\nDETAIL: no code\nFIELDS_FILLED_JSON: {}\nsession_id: s1\n",
        ]
    )
    with patch.object(otp_lookup, "wait_for_code", side_effect=fake_wait), patch.object(
        agent_driver, "_run_hermes", side_effect=lambda **kw: (next(rounds), False)
    ), patch.object(agent_driver, "session_api_calls", return_value=3), patch.object(
        agent_driver, "_dump_debug_transcript"
    ), patch.object(agent_driver, "hermes_profile", return_value="jobo"), patch.object(
        agent_driver,
        "_open_page_urls",
        create=True,
        side_effect=lambda cdp: ("https://treyco.wd1.myworkdayjobs.com/en-US/External/job/1",),
    ):
        try:
            agent_driver.run_apply_agent(
                system_prompt="S",
                user_prompt="U",
                cdp_url="http://127.0.0.1:1",
                job_id=1429,
                source_url="https://www.linkedin.com/jobs/view/4000000105/",
                company="Treyco",
            )
        except TypeError as e:
            seen.append({"error": str(e)})
    got = seen[0].get("company") if seen else None
    out.append(("run_apply_agent_passes_company_to_lookup", "Treyco", got))
    urls = tuple(seen[0].get("employer_urls") or ()) if seen else ()
    out.append(
        (
            "run_apply_agent_passes_job_url_to_lookup",
            True,
            "https://www.linkedin.com/jobs/view/4000000105/" in urls,
        )
    )
    out.append(
        (
            "open_tab_urls_reach_lookup",
            True,
            "https://treyco.wd1.myworkdayjobs.com/en-US/External/job/1" in urls,
        )
    )
    # A LinkedIn/Google request is refused before the browser is touched.
    touched: list[str] = []
    with patch.object(otp_lookup, "wait_for_code", side_effect=fake_wait), patch.object(
        agent_driver, "_open_page_urls", create=True, side_effect=lambda cdp: touched.append(cdp) or ()
    ):
        try:
            agent_driver._resolve_otp("linkedin.com", "", 1429, company="Treyco", cdp_url="http://127.0.0.1:1")
        except TypeError:
            pass
    out.append(("linkedin_refusal_reads_no_tabs", [], touched))
    src = inspect.getsource(runner)
    out.append(("runner_threads_company_name", True, "company=str(job.company_name" in src))
    return out


def main() -> int:
    mod = _load(REPO / "module4" / "otp_lookup.py", "otp_lookup_employer_under_test")
    failed = 0
    total = 0
    for title, rows in (
        ("mailbox scenarios (fake Gmail)", mailbox_cases(mod)),
        ("employer name units", unit_cases(mod)),
        ("apply driver threading", driver_cases()),
    ):
        print(f"\n  -- {title} --")
        for label, expected, got in rows:
            ok = got == expected
            total += 1
            failed += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {label:50s} expected={expected!r} got={got!r}")
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
