"""Offline replay of the HALV / Workday account-verification miss (job 1523).

What happened (run_20261014_1000.log 11:17-11:19): the agent asked
`NEED_OTP: myworkdayjobs.com | Verify your account`. Workday mailed, twice:
    From: HALV Workday HCM <halv@otp.workday.com>
    Subject: Verify your candidate account
    Body: "Click this link to confirm your email address and complete setup
           for your candidate account https://halv.wd3.myworkdayjobs.com/
           HALV_Careers/activate/<token>/?redirect=..."
There is no code. `from:myworkdayjobs.com` never matched the sender, and the
unfiltered pass saw the mail on every poll but skipped it: the link has the
token as a path segment, not `token=`, and none of the sign-in wording matched.

This suite proves the activation link is now returned, the sender-filtered
pass finds Workday mail even when the inbox is busy, and the old guards hold.
Fixture is the real mail with personal data and the token replaced.

No network, no Gmail, no browser: Gmail is a fake service.

Run:  .venv/bin/python scripts/test_otp_workday_activation.py
"""

from __future__ import annotations

import base64
import importlib.util
import re
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

HALV_LINK = (
    "https://halv.wd3.myworkdayjobs.com/HALV_Careers/activate/"
    "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7/"
    "?redirect=%2Fen-US%2FHALV_Careers%2Fjob%2FNew-Delhi%252C-India%2F"
    "Software-Engineer_R00000000-1%2Fapply%2FapplyManually"
)
HALV_VERIFY = {
    "from": "HALV Workday HCM <halv@otp.workday.com>",
    "subject": "Verify your candidate account",
    "body": (
        " \r\n \r\n body, table {\r\n font-family: Verdana, Arial, sans-serif;\r\n"
        " font-size: 12px; width:100%;\r\n}\r\n"
        " Click this link to confirm your email address and complete setup for your candidate\r\n"
        f" account {HALV_LINK} The link will expire after hours. \r\n \r\n \r\n"
        " This email was intended for candidate@example.com \r\n \r\n"
        " This message contains information which may be confidential, proprietary, privileged,\r\n"
        " or otherwise protected by law from disclosure or use by a third party. Please visit HALV's\r\n"
        " web site: http://www.halv.example · HALV | Example footer\r\n"
    ),
    "headers": {
        "Return-Path": "<0102-bounce@eu-west-1.bounce.otp.workday.com>",
        "Reply-To": "hris.alerts@halv.example",
    },
}
# The distractor the unfiltered pass logged on every poll that morning.
LINKEDIN_SENT = {
    "from": "LinkedIn <jobs-noreply@linkedin.com>",
    "subject": "Alex, your application was sent to Linnet",
    "body": "Your application was sent to Linnet\r\nhttps://www.linkedin.com/comm/jobs/view/1/\r\n",
}
# Workday also sends codes for some tenants; a code still wins over the link.
WORKDAY_CODE = {
    "from": "Acme Workday HCM <acme@otp.workday.com>",
    "subject": "Verify your email",
    "body": "Please verify your email. Your verification code is 482915.\r\n",
}
NEWSLETTER = {
    "from": "Product News <news@somesaas.io>",
    "subject": "What's new this week",
    "body": "Try the new dashboard https://app.somesaas.io/activate/abcdefghijklmnopqrstuvwxyz\r\n",
}


# --- fake Gmail -----------------------------------------------------------------


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
        """Gmail semantics for what find_code sends: `from:x`, `from:(a OR b)`
        (domain matches the address domain or a subdomain of it), `subject:"..."`."""
        hdr = {h["name"].lower(): h["value"].lower() for h in m["payload"]["headers"]}
        addr = re.search(r"@([a-z0-9.-]+)", hdr.get("from", ""))
        dom = addr.group(1) if addr else ""
        for group, single in re.findall(r"from:\(([^)]*)\)|from:(\S+)", q):
            alts = [a.strip().lower() for a in group.split(" OR ")] if group else [single.lower()]
            if not any(dom == a or dom.endswith("." + a) for a in alts):
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


ASK = dict(sender="myworkdayjobs.com", subject_hint="Verify your account")


def lookup(mod: ModuleType, inbox: FakeGmail, **kw):
    mod._gmail_service = lambda: inbox
    args = dict(ASK, minutes=10, company="HALV")
    args.update(kw)
    return mod.find_code(**args)


# --- cases ----------------------------------------------------------------------


def mailbox_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    out = []

    # 1. The 2026-10-14 inbox: both HALV mails, LinkedIn receipt newest.
    g = FakeGmail()
    g.add(HALV_VERIFY, age_sec=60)
    g.add(HALV_VERIFY, age_sec=25)
    g.add(LINKEDIN_SENT, age_sec=5)
    r = lookup(mod, g)
    out.append(("halv_activation_link_returned", HALV_LINK, r.magic_link))
    out.append(("halv_no_code_invented", None, r.code))
    out.append(("halv_found_by_sender_filtered_pass", True, "workday.com" in r.searched_query))

    # 2. Busy inbox: 20 newer unrelated mails push HALV out of the unfiltered
    #    pass's 15-message page. Only the Workday sender filter can reach it.
    g = FakeGmail()
    g.add(HALV_VERIFY, age_sec=90)
    for i in range(20):
        g.add({"from": f"Shop {i} <deals@shop{i}.com>", "subject": "Sale", "body": "50% off"}, age_sec=60 - i)
    out.append(("halv_link_found_in_busy_inbox", HALV_LINK, lookup(mod, g).magic_link))

    # 3. Mail older than the OTP request: ignored.
    g = FakeGmail()
    g.add(HALV_VERIFY, age_sec=200)
    r = lookup(mod, g, not_before=datetime.now(timezone.utc) - timedelta(seconds=60))
    out.append(("halv_mail_before_not_before_ignored", ("", None), (r.magic_link, r.code)))

    # 4. A Workday tenant that mails a real code: the code wins.
    g = FakeGmail()
    g.add(WORKDAY_CODE, age_sec=20)
    out.append(("workday_code_still_preferred", "482915", lookup(mod, g, company="Acme").code))

    # 5. Agent asked for a different site: the Workday link is not handed over.
    g = FakeGmail()
    g.add(HALV_VERIFY, age_sec=20)
    r = lookup(mod, g, sender="greenhouse.io", subject_hint="", company="Linnet")
    out.append(("workday_link_refused_for_other_site", "", r.magic_link))
    return out


def unit_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    wd = "from:(myworkdayjobs.com OR myworkdaysite.com OR myworkday.com OR workday.com)"
    return [
        ("filter_widened_for_myworkdayjobs", wd, mod._from_filter("myworkdayjobs.com")),
        ("filter_widened_for_tenant_host", wd, mod._from_filter("halv.wd3.myworkdayjobs.com")),
        ("filter_widened_for_myworkday", wd, mod._from_filter("myworkday.com")),
        ("filter_unchanged_for_other_sites", "from:northwind.com", mod._from_filter("northwind.com")),
        ("filter_not_fooled_by_suffix_lookalike", "from:notworkday.com", mod._from_filter("notworkday.com")),
        ("link_from_real_body", HALV_LINK, mod.extract_magic_link(HALV_VERIFY["body"], HALV_VERIFY["subject"])),
        ("newsletter_activate_link_needs_wording", "", mod.extract_magic_link(NEWSLETTER["body"], NEWSLETTER["subject"])),
        (
            "short_activate_segment_not_a_token",
            "",
            mod.extract_magic_link("Confirm your email: https://x.io/activate/abc", ""),
        ),
    ]


def main() -> int:
    mod = _load(REPO / "module4" / "otp_lookup.py", "otp_lookup_workday_under_test")
    failed = 0
    total = 0
    for title, rows in (
        ("mailbox scenarios (fake Gmail)", mailbox_cases(mod)),
        ("filter and link units", unit_cases(mod)),
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
