"""Offline replay of the 2026-09-29 Humongo wrong-code failure (job 1007).

What happened (run_20260929_1659.log, Hermes session 20260929_210951_36e7f2):
  21:03:53  job 993 (Cedarline) submitted via LinkedIn Easy Apply;
            LinkedIn mails "Alex, your application was sent to Brightwave
            Staffing" from jobs-noreply@linkedin.com.
  21:10:28  job 1007 (Vanarsdel, Paradox.ai) clicks "Email" on
            humongo.paradox.ai/candidate-experience/verification-code
  21:10:30  agent prints `NEED_OTP: paradox.ai | Humongo verification code`
  21:10:32  filtered search `from:paradox.ai subject:"Humongo verification code"`
            matches nothing; the unfiltered fallback returns "qz7k2m" from
            the LinkedIn mail. It is typed into Humongo's page; Humongo rejects it.
  21:12:09  same again ("..., second request") -> "qz7k2m" again.
  21:14:01  third request: the LinkedIn mail is now >10 min old, nothing found.

Why "qz7k2m": every LinkedIn mail body carries tracking links such as
  ...-view_job-null-qz7k2m~xp4mtrlc~w7-null-null&eid=qz7k2m-xp4mtrlc-w7&otpToken=...
(see data/email_samples.json: the same token appears in 70+ LinkedIn mails).
The substring "otp" inside "otpToken" counted as verification wording, and
"qz7k2m" sat inside the 40-char window before it.

No network, no Gmail, no browser: Gmail is replaced by a fake service.

Run:      .venv/bin/python scripts/test_otp_sender_guard.py
Old code: .venv/bin/python scripts/test_otp_sender_guard.py --old <path-to-old-otp_lookup.py>
          (e.g. `git show 8aa2254:module4/otp_lookup.py > /tmp/old.py`); prints
          the old module's answers next to the new ones, for comparison only.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- fixtures -----------------------------------------------------------------

# Shape copied from the real LinkedIn "application was sent" mails in
# data/email_samples.json (sample mails), company swapped for the one
# job 993 applied to. The tracking token is the member-level "qz7k2m" seen in
# every LinkedIn sample, i.e. the exact string that reached Humongo's page.
_LI_TRACK = (
    "lipi=urn%3Ali%3Apage%3Aemail_email_application_confirmation_with_nba_01%3BGXWyybWhHgqNZT8ggkwMMY%3D%3D"
    "&midToken=OEWTAnAk7M6EHs&midSig=11QuoRuA30nlx0"
    "&trk=eml-email_application_confirmation_with_nba_01-application~confirmation-0-view_job"
    "&trkEmail=eml-email_application_confirmation_with_nba_01-application~confirmation-0-view_job"
    "-null-qz7k2m~xp4mtrlc~w7-null-null&eid=qz7k2m-xp4mtrlc-w7"
    "&otpToken=FAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOKENFAKEOTPTOK%3D%3D"
)
LINKEDIN_APPLIED_MAIL = {
    "from": "LinkedIn <jobs-noreply@linkedin.com>",
    "subject": "Alex, your application was sent to Cedarline",
    "body": (
        "Your application was sent to Cedarline\r\n\r\n"
        "Software Engineer (Example)\r\nCedarline\r\nUnited States\r\n"
        f"View job: https://www.linkedin.com/comm/jobs/view/4400000000/?trackingId=CPLXkU46NN1JTAqsS4mKyS%3D%3D&{_LI_TRACK}\r\n\r\n"
        "---------------------------------------------------------\r\n  \r\n"
        "Applied on September 29, 2026\r\n"
        "        Now, take these next steps for more success\r\n\r\n"
        "View similar jobs you may be interested in\r\n            \r\n"
        "Intern\r\nHumongo\r\nMumbai Metropolitan Region\r\n"
        f"View job: https://www.linkedin.com/comm/jobs/view/4000000103/?trackingId=3%2BIM0NRMOQsohLAdB6XKzG%3D%3D&{_LI_TRACK}\r\n"
    ),
}

# The real Paradox code mail was never captured (it was not found by any
# lookup on 29 Sep and we do not read the user's Gmail). These are ASSUMED shapes
# that follow the page text "We just sent a code to a***@e***.com".
PARADOX_CODE = {
    "from": "Humongo Careers <noreply@paradox.ai>",
    "subject": "Your verification code",
    "body": (
        "Hi Alex,\r\n\r\nYour verification code is 482913. It expires in 10 minutes.\r\n\r\n"
        "Complete your form: https://humongo.paradox.ai/candidate-experience/verification-code\r\n"
    ),
}
# Variant: relayed through an email provider, so only the display name and
# the body's links show who sent it.
PARADOX_CODE_RELAYED = {
    "from": "Humongo Recruiting <bounce-7731@em8810.sendgrid.net>",
    "subject": "Check your email",
    "body": (
        "Please enter the code below to continue.\r\n\r\nYour code: 482913\r\n\r\n"
        "Powered by Paradox https://humongo.paradox.ai/\r\n"
    ),
}

# Real shapes from earlier runs (sender + subject verbatim from the logs;
# bodies minimal). These lookups worked before and must keep working.
GOOD_PATHS = [
    # (label, NEED_OTP sender, hint, mail, expected code)
    (
        "wrens_successfactors_782",
        "wrens.com",
        "one-time password",
        {
            "from": "\"Wren's Talent Attraction\" <donotreply@successfactors.com>",
            "subject": "One-Time Password for Candidate Account Verification",
            "body": "Your one-time password is 518204. It is valid for 15 minutes.\n",
        },
        "518204",
    ),
    (
        "yewdale_oracle_1068",
        "oraclecloud.com",
        "Confirm Your Identity verification code",
        {
            "from": "Yewdale Ltd <YEWORC@workflow.mail.ap1.cloud.oracle.com>",
            "subject": "Confirm your identity",
            "body": "Use this verification code to confirm your identity: 730215\n",
        },
        "730215",
    ),
    (
        "zorvik_unknown_sender_1349",
        "unknown",
        "Enter confirmation code for alex.example@example.com",
        {
            "from": "Zorvik <support@zorvik.so>",
            "subject": "820317 is your confirmation code for Zorvik",
            "body": "Enter this code to confirm your email.\n",
        },
        "820317",
    ),
    (
        "blueyon_alt_domain_797",
        "blueyonfirst.bank.in",
        "Blueyon application OTP",
        {
            "from": "Blueyon <alerts@ta.blueyonfirstbank.com>",
            "subject": "Complete your job application at Blueyon NOW!",
            "body": "Your OTP to continue your application is 664120.\n",
        },
        "664120",
    ),
    (
        "amazon_jobs_743",
        "amazon.jobs",
        "verification code",
        {
            "from": "\"Amazon.jobs\" <noreply@mail.amazon.jobs>",
            "subject": "Your amazon.jobs verification code",
            "body": "Your verification code is 905517.\n",
        },
        "905517",
    ),
    (
        "shine_board_own_code_387",
        "shine.com",
        "Shine job application verification code",
        {
            "from": "\"Shine.com\" <jobs@jobsalert.shine.com>",
            "subject": "Your OTP for Shine.com login",
            "body": "Your OTP for login is 271903.\n",
        },
        "271903",
    ),
    (
        "godwit_workos_478",
        "authkit.app",
        "Sign in code from plinth-otter-41.authkit.app (Hollyby login)",
        {
            "from": "Hollyby <access@workos-mail.com>",
            "subject": "Sign up for Hollyby",
            "body": "Your sign-in code is 448190.\n",
        },
        "448190",
    ),
    (
        "turbohire_code_in_subject_594",
        "turbohire.co",
        "Verify your email",
        {
            "from": "TurboHire <no-reply@turbohire.co>",
            "subject": "6-digit Code for your application to Birchway - 639254",
            "body": "Use the code in the subject line.\n",
        },
        "639254",
    ),
    (
        "greenhouse_mixed_case_798",
        "greenhouse.io",
        "verification code Greenhouse job application",
        {
            "from": "Greenhouse <no-reply@us.greenhouse-mail.io>",
            "subject": "Security code for your application to VeQ",
            "body": "Copy and paste this code into the security code field on your "
            "application:\n\nR3hTKGwN\n\n",
        },
        "R3hTKGwN",
    ),
    (
        "linkedin_own_pin_1007_oct3",
        "linkedin.com",
        "Enter the 6-digit code",
        {
            "from": "LinkedIn <security-noreply@linkedin.com>",
            "subject": "Alex, here's your PIN 417092",
            "body": "Hi Alex,\n\nPlease use this verification code to complete your sign in: 417092\n",
        },
        "417092",
    ),
    (
        # Sender-filtered pass with the guessed subject dropped: a templated
        # code block with no wording next to it (as SuccessFactors/Workday do).
        "workday_styled_block_wrong_subject_guess",
        "myworkday.com",
        "Verify your Fourthco account",
        {
            "from": "Fourthco <fourthco@myworkday.com>",
            "subject": "Your one-time code",
            "body": "Hello Alex,\n\n   903117\n\nThis code expires in 15 minutes.\n",
        },
        "903117",
    ),
]


# --- fake Gmail -----------------------------------------------------------------


class FakeGmail:
    """Just enough of the Gmail API for otp_lookup: list(q) + get(id).

    Query support mirrors what otp_lookup sends: `from:<x>` (substring of the
    From header), `subject:"<phrase>"` (case-insensitive substring of the
    subject); newer_than is ignored (otp_lookup filters on internalDate).
    Messages are returned newest first, like Gmail.
    """

    def __init__(self):
        self.msgs: list[dict] = []
        self.queries: list[str] = []

    def add(self, mail: dict, *, age_sec: float, extra_headers: dict | None = None) -> None:
        received = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        headers = [
            {"name": "From", "value": mail["from"]},
            {"name": "Subject", "value": mail["subject"]},
        ]
        for k, v in (extra_headers or {}).items():
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

    # chained API: service.users().messages().list(...).execute()
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
        import re

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


def lookup(mod: ModuleType, inbox: FakeGmail, sender: str, hint: str, **kw):
    mod._gmail_service = lambda: inbox
    return mod.find_code(sender=sender, minutes=10, subject_hint=hint, **kw)


# --- cases ------------------------------------------------------------------------


def scenario_results(mod: ModuleType, *, is_new: bool) -> list[tuple[str, object, object]]:
    """(label, expected, got) for every scenario, run against `mod`."""
    out = []

    # A. The exact 29 Sep state at 21:10:32: only the LinkedIn mail (sent
    #    21:03:55, ~6.5 min earlier) is in the window; Humongo's mail has not
    #    landed yet.
    for hint in (
        "Humongo verification code",
        "Humongo verification code, second request",
    ):
        g = FakeGmail()
        g.add(LINKEDIN_APPLIED_MAIL, age_sec=397)
        r = lookup(mod, g, "paradox.ai", hint)
        out.append((f"0929_linkedin_only[{hint}]", None, r.code))

    # A2. Same inbox, agent had no idea of the sender.
    g = FakeGmail()
    g.add(LINKEDIN_APPLIED_MAIL, age_sec=397)
    out.append(("0929_linkedin_only[NEED_OTP: unknown]", None, lookup(mod, g, "unknown", "").code))

    # B. Humongo's real code mail present alongside the LinkedIn mail. The
    #    agent's subject guess still matches nothing.
    for label, mail in (("paradox_direct", PARADOX_CODE), ("paradox_relayed", PARADOX_CODE_RELAYED)):
        g = FakeGmail()
        g.add(LINKEDIN_APPLIED_MAIL, age_sec=397)
        g.add(mail, age_sec=20)
        r = lookup(mod, g, "paradox.ai", "Humongo verification code")
        out.append((f"0929_with_{label}", "482913", r.code))

    # B2. LinkedIn mail is the NEWEST message (arrived after the Humongo mail).
    g = FakeGmail()
    g.add(PARADOX_CODE, age_sec=40)
    g.add(LINKEDIN_APPLIED_MAIL, age_sec=5)
    out.append(
        ("0929_linkedin_newer_than_paradox", "482913", lookup(mod, g, "paradox.ai", "Humongo verification code").code)
    )

    # C. Polling: first poll sees only LinkedIn, Humongo's mail lands before
    #    the second poll. The first answer is the one the agent types.
    g = FakeGmail()
    g.add(LINKEDIN_APPLIED_MAIL, age_sec=397)
    mod._gmail_service = lambda: g
    orig_sleep = mod.time.sleep
    mod.time.sleep = lambda s: g.add(PARADOX_CODE, age_sec=1)
    try:
        r = mod.wait_for_code(
            sender="paradox.ai", subject_hint="Humongo verification code", timeout_sec=30, poll_interval_sec=1
        )
    finally:
        mod.time.sleep = orig_sleep
    out.append(("0929_poll_until_paradox_arrives", "482913", r.code))

    # D. A right-looking code from an OTHER site must not be used either.
    g = FakeGmail()
    g.add(
        {
            "from": "Adatum <adatum@myworkday.com>",
            "subject": "Verify your email address",
            "body": "Your verification code is 903117.\n",
        },
        age_sec=60,
    )
    out.append(("other_site_code_not_used", None, lookup(mod, g, "paradox.ai", "Humongo verification code").code))

    # E. Stale mail: a genuine Humongo code from an EARLIER request (job 1005
    #    ran just before 1007) must not be reused. Needs not_before (new API).
    if is_new:
        g = FakeGmail()
        g.add(PARADOX_CODE, age_sec=120)
        r = lookup(
            mod,
            g,
            "paradox.ai",
            "",
            not_before=datetime.now(timezone.utc) - timedelta(seconds=60),
        )
        out.append(("stale_code_before_request_ignored", None, r.code))

    # F. Paths that already worked (logs 2026-09-03 .. 2026-10-03), each with
    #    the LinkedIn notification also in the inbox as a distractor.
    for label, sender, hint, mail, expected in GOOD_PATHS:
        g = FakeGmail()
        g.add(LINKEDIN_APPLIED_MAIL, age_sec=30)
        g.add(mail, age_sec=10)
        out.append((label, expected, lookup(mod, g, sender, hint).code))

    return out


def unit_results(mod: ModuleType) -> list[tuple[str, object, object]]:
    """sender_rejection / extraction units (new module only)."""
    out = []
    S = mod.SiteHint.parse
    rej = mod.sender_rejection
    li = LINKEDIN_APPLIED_MAIL
    out.append(("linkedin_rejected_for_paradox", True, bool(rej(li["from"], li["body"], S("paradox.ai", "Humongo verification code")))))
    out.append(("linkedin_rejected_for_unknown", True, bool(rej(li["from"], li["body"], S("unknown", "")))))
    out.append(("jobs_noreply_rejected_even_for_linkedin", True, bool(rej(li["from"], li["body"], S("linkedin.com", "")))))
    out.append(("naukri_rejected_for_other_site", True, bool(rej("Naukri <info@naukri.com>", "", S("northwind.com", "")))))
    out.append(("paradox_from_accepted", False, bool(rej(PARADOX_CODE["from"], PARADOX_CODE["body"], S("paradox.ai", "")))))
    out.append(("paradox_relayed_body_link_accepted", False, bool(rej(PARADOX_CODE_RELAYED["from"], PARADOX_CODE_RELAYED["body"], S("paradox.ai", "")))))
    out.append(("xy_does_not_match_galaxy", True, bool(rej("Galaxy <hi@galaxy.com>", "", S("xy.com", "")))))
    # Extraction: URL tracking tokens are never codes, even next to "otp".
    out.append(("otpToken_url_not_a_code", None, mod.extract_code(li["body"], subject=li["subject"], require_context=True)))
    return out


def sample_scan(mod: ModuleType) -> dict:
    """Strict extraction over the real collector mail samples in the repo."""
    path = REPO / "data" / "email_samples.json"
    if not path.exists():
        return {}
    items = json.loads(path.read_text())
    li_hits, other_hits = 0, []
    for it in items:
        body = it.get("body_text") or it.get("body_html") or ""
        code = mod.extract_code(body, subject=it.get("subject", ""), require_context=True)
        if not code:
            continue
        if "linkedin" in it.get("sender", "").lower():
            li_hits += 1
        else:
            other_hits.append((it.get("sender", "")[:40], it.get("subject", "")[:50], code))
    return {"total": len(items), "linkedin_codes": li_hits, "other": other_hits}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", help="path to an old otp_lookup.py to compare against")
    args = ap.parse_args()

    new = _load(REPO / "module4" / "otp_lookup.py", "otp_lookup_new")
    old = _load(Path(args.old), "otp_lookup_old") if args.old else None

    failed = 0
    rows = scenario_results(new, is_new=True)
    old_rows = {lbl: got for lbl, _, got in scenario_results(old, is_new=False)} if old else {}
    print("  -- mailbox scenarios (fake Gmail) --")
    for label, expected, got in rows:
        ok = got == expected
        failed += not ok
        extra = f"   old={old_rows[label]!r}" if label in old_rows else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {label:45s} expected={expected!r} got={got!r}{extra}")

    print("\n  -- sender guard / extraction units --")
    for label, expected, got in unit_results(new):
        ok = got == expected
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label:45s} expected={expected!r} got={got!r}")

    scan = sample_scan(new)
    if scan:
        ok = scan["linkedin_codes"] == 0
        failed += not ok
        line = f"strict codes from {scan['total']} real collector mails: linkedin={scan['linkedin_codes']}"
        if old:
            o = sample_scan(old)
            line += f" (old: linkedin={o['linkedin_codes']}, other={len(o['other'])})"
        print(f"\n  {'PASS' if ok else 'FAIL'}  {line}")
        for s, subj, code in scan["other"]:
            print(f"          non-LinkedIn hit (review): {s!r} {subj!r} -> {code!r}")

    total = len(rows) + len(unit_results(new)) + (1 if scan else 0)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
