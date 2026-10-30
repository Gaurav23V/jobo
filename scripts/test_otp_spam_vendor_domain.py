"""Offline replay: an amazon.jobs sign-in code that arrived, in Spam (GHO-102).

Job 2897, 2026-10-30: the agent asked twice `NEED_OTP: amazon.jobs | Verify
your email`. Both codes arrived within seconds, as
    From: noreply@portal-na.idprism-auth.amazon.dev  (sent via amazonses.com)
    Subject: Your Amazon A to Z verification code
    text/html only: "<code> is your Amazon A to Z verification code. ...
                     Your verification code is: <code>"
and Gmail filed both under Spam. The sender-filtered pass searched
`from:amazon.jobs` (wrong domain) and the unfiltered pass searched Gmail's
default scope, which skips Spam, so both requests timed out empty.

The fake Gmail below models that: Spam mail is only listed when the query
says `in:anywhere`, Trash only with `in:trash`. Codes are replaced.
No network, no Gmail, no browser.

Run through scripts/run_regression_tests.sh (not directly).
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


A2Z_FROM = "noreply@portal-na.idprism-auth.amazon.dev"
A2Z_SUBJECT = "Your Amazon A to Z verification code"


def a2z_html(code: str) -> str:
    """The real mail's shape: html only, style block, code twice."""
    return (
        "<html><head><title>Amazon A to Z verification code</title>"
        "<style>body { margin:0; padding:0; } .code { font-size:28px !important;"
        " letter-spacing:6px !important; }</style></head><body>"
        f'<div style="display:none">{code} is your Amazon A to Z verification code.</div>'
        "<table><tr><td>Hi,</td></tr><tr><td>Your verification code is:</td></tr>"
        f'<tr><td class="code">{code}</td></tr>'
        "<tr><td>This code expires in 5 minutes .</td></tr>"
        "<tr><td>Don't reply to this email or share this code with anyone. Amazon A to Z "
        "will never ask you for this code.</td></tr></table></body></html>"
    )


def a2z_mail(code: str) -> dict:
    return {
        "from": A2Z_FROM,
        "subject": A2Z_SUBJECT,
        "mime": "text/html",
        "body": a2z_html(code),
        "headers": {"Return-Path": "<0100abc-000000@amazonses.com>"},
    }


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeGmail:
    """Gmail list/get with folder scope: default search skips SPAM and TRASH."""

    def __init__(self):
        self.msgs: list[dict] = []
        self.queries: list[str] = []

    def add(self, mail: dict, *, age_sec: float, label: str = "INBOX") -> None:
        received = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        headers = [
            {"name": "From", "value": mail["from"]},
            {"name": "Subject", "value": mail["subject"]},
        ]
        headers += [{"name": k, "value": v} for k, v in (mail.get("headers") or {}).items()]
        self.msgs.append(
            {
                "id": f"m{len(self.msgs)}",
                "labelIds": [label],
                "internalDate": str(int(received.timestamp() * 1000)),
                "payload": {
                    "mimeType": mail.get("mime", "text/plain"),
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
        label = m["labelIds"][0]
        if label == "TRASH" and not re.search(r"(?<!-)in:trash", q):
            return False
        if label == "SPAM" and "in:anywhere" not in q and "in:spam" not in q:
            return False
        if label == "TRASH" and "-in:trash" in q:
            return False
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


ASK = dict(sender="amazon.jobs", subject_hint="Verify your email", company="Fabrikam")
NOISE = [
    {"from": "LinkedIn <jobs-noreply@linkedin.com>", "subject": "Your application was sent to Coho",
     "body": "Your application was sent to Coho.\n"},
    {"from": "noreply@candidates.workablemail.com", "subject": "Thanks for applying to Litware",
     "body": "Thanks for applying. Reference 482915.\n"},
]


def lookup(mod: ModuleType, inbox: FakeGmail, ask: dict = ASK, **kw):
    mod._gmail_service = lambda: inbox
    args = dict(ask, minutes=10)
    args.update(kw)
    return mod.find_code(**args)


def mailbox_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    out = []

    # 1. The 2026-10-30 inbox: the code in Spam, newer receipts in the inbox.
    g = FakeGmail()
    g.add(a2z_mail("604317"), age_sec=40, label="SPAM")
    for i, n in enumerate(NOISE):
        g.add(n, age_sec=10 + i)
    r = lookup(mod, g)
    out.append(("spam_code_found_for_amazon_jobs_request", "604317", r.code))
    out.append(("found_by_sender_filtered_pass", True, "amazon.dev" in r.searched_query))

    # 2. Second request: only the code mailed after it counts.
    g = FakeGmail()
    g.add(a2z_mail("604317"), age_sec=150, label="SPAM")
    g.add(a2z_mail("288150"), age_sec=20, label="SPAM")
    r = lookup(mod, g, not_before=datetime.now(timezone.utc) - timedelta(seconds=60))
    out.append(("second_request_reads_second_code", "288150", r.code))

    # 3. Same mail delivered to the inbox: still found.
    g = FakeGmail()
    g.add(a2z_mail("604317"), age_sec=20)
    out.append(("inbox_code_still_found", "604317", lookup(mod, g).code))

    # 4. The agent named the host with www.
    g = FakeGmail()
    g.add(a2z_mail("604317"), age_sec=20, label="SPAM")
    out.append(("www_host_request_finds_code", "604317",
                lookup(mod, g, dict(ASK, sender="www.amazon.jobs")).code))

    # 5. Spam from another sender is never read for an amazon.jobs request.
    g = FakeGmail()
    g.add({"from": "security@login-check.example", "subject": "Your verification code",
           "body": "Your verification code is: 771204"}, age_sec=20, label="SPAM")
    out.append(("other_sender_spam_refused", None, lookup(mod, g).code))

    # 6. No site named: the unfiltered search stays out of Spam.
    g = FakeGmail()
    g.add({"from": "security@login-check.example", "subject": "Your verification code",
           "body": "Your verification code is: 771204"}, age_sec=20, label="SPAM")
    r = lookup(mod, g, dict(sender="", subject_hint="", company="Fabrikam"))
    out.append(("unfiltered_search_skips_spam", None, r.code))
    out.append(("unfiltered_query_has_no_spam_scope", True,
                all("in:anywhere" not in q for q in g.queries)))

    # 7. Trash is never searched.
    g = FakeGmail()
    g.add(a2z_mail("604317"), age_sec=20, label="TRASH")
    out.append(("trash_never_searched", None, lookup(mod, g).code))

    # 8. The amazon.jobs code is not handed to a request for another site.
    g = FakeGmail()
    g.add(a2z_mail("604317"), age_sec=20, label="SPAM")
    r = lookup(mod, g, dict(sender="greenhouse.io", subject_hint="", company="Fabrikam"))
    out.append(("amazon_code_refused_for_other_site", None, r.code))
    return out


def unit_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    az = "from:(amazon.jobs OR amazon.dev)"
    wd = "from:(myworkdayjobs.com OR myworkdaysite.com OR myworkday.com OR workday.com)"
    return [
        ("filter_widened_for_amazon_jobs", az, mod._from_filter("amazon.jobs")),
        ("filter_widened_for_www_amazon_jobs", az, mod._from_filter("www.amazon.jobs")),
        ("filter_widened_for_amazon_dev_host", az,
         mod._from_filter("portal-na.idprism-auth.amazon.dev")),
        ("filter_not_fooled_by_lookalike", "from:notamazon.jobs", mod._from_filter("notamazon.jobs")),
        ("workday_filter_unchanged", wd, mod._from_filter("myworkdayjobs.com")),
        ("filtered_query_covers_spam_not_trash", True,
         mod._build_query(sender="dice.com", minutes=10, subject_hint="").endswith(
             "from:dice.com in:anywhere -in:trash")),
        ("unfiltered_query_unchanged", "newer_than:1d",
         mod._build_query(sender="", minutes=10, subject_hint="")),
    ]


def main() -> int:
    mod = _load(REPO / "module4" / "otp_lookup.py", "otp_lookup_spam_under_test")
    failed = 0
    total = 0
    for title, rows in (
        ("mailbox scenarios (fake Gmail)", mailbox_cases(mod)),
        ("filter and query units", unit_cases(mod)),
    ):
        print(f"\n  -- {title} --")
        for label, expected, got in rows:
            ok = got == expected
            total += 1
            failed += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {label}: expected={expected!r} got={got!r}")
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
