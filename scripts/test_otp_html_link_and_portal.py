"""Offline replay of two verification mails that arrived but were never read.

Job 2201 (Dice), 2026-10-23/24, three attempts: the agent asked
`NEED_OTP: dice.com | Verify your email`. Dice mailed, every time:
    From: no-reply@dice.com
    Subject: Dice.com - Verify Candidate Email
    multipart/alternative: an EMPTY text/plain part + the real text/html part
    with one button <a href="https://www.dice.com/account/<token>?u=...">Verify Email</a>
_walk_body preferred the blank "plain" part, so the body read as "". With the
HTML, tag stripping dropped the href, and the bare URL matched no link pattern.

Job 2089 (Datum, a Salesforce Sites portal at <tenant>.my.salesforce-sites.com),
2026-10-24: the agent asked `NEED_OTP: salesforce-sites.com | Enter
authentication code`. The code came as
    From: Datum Careers <careers@careers.datum.example>
    Subject: Datum Careers - Candidate registration code
    "... use this 6-digit code ... Your code: <digits>"
and the open pass skipped it: salesforce-sites was not a white-label vendor,
so the employer's own mail did not count as the requested site.

Fixtures keep the real MIME shapes; names, codes and tokens are replaced.
No network, no Gmail, no browser: Gmail is a fake service.

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


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"size": len(text), "data": _b64(text)}}


def _multi(mime: str, *parts: dict) -> dict:
    return {"mimeType": mime, "body": {"size": 0}, "parts": list(parts)}


# --- fixtures -----------------------------------------------------------------

DICE_LINK = (
    "https://www.dice.com/account/q7Zr2LmX9vKp4TnB8sWd?u=aB3dE5fG7hJ9kL1mN3pQ5rS7tU9v"
    "&c=000000&$deep_link=true"
)


def _dice_html(button_href: str, button_label: str = "Verify Email") -> str:
    href = button_href.replace("&", "&amp;")
    return (
        "<!doctype html><html><head><title></title><style type=\"text/css\">"
        "#outlook a { padding:0; } body { margin:0;padding:0; } "
        "p { display:block;margin:13px 0; }</style></head><body>"
        "<table><tr><td><p>Hi Alex,</p><p>Thanks for choosing Dice! To complete your "
        "account setup, please click the verification link below:</p></td></tr>"
        f"<tr><td><a href=\"{href}\" style=\"color:#fff\" target=\"_blank\">\n"
        f"  <span>{button_label}</span>\n</a></td></tr>"
        "<tr><td><p>If you didn&#8217;t sign up for a Dice account, you can safely ignore "
        "this email.</p><p>Need help? Feel free to reach out to our "
        "<a href=\"mailto:support@dice.example\">support team</a>.</p>"
        "<p>Best regards,<br>The Dice.com Team</p></td></tr>"
        "<tr><td><a href=\"http://click.dice.com/f/a/Zx9Yw8Vu7Ts6Rq5Po4Nm/AAAAARA~/Lk3Jh2Gf1Ds0\">"
        "Facebook</a> <a href=\"http://click.dice.com/f/a/Mn4Bv5Cx6Za7Sd8Fg9Hj/AAAAARA~/Qw1Er2Ty3Ui4\">"
        "Privacy Policy</a></td></tr></table></body></html>"
    )


def dice_mail(html: str, from_hdr: str = "no-reply@dice.com") -> dict:
    """The real Dice shape: an empty text/plain part next to the HTML."""
    return {
        "headers": {
            "From": from_hdr,
            "Subject": "Dice.com - Verify Candidate Email",
            "Return-Path": "<msprvs1=bounces-1@dhi.mail.e.sparkpost.com>",
        },
        "payload": _multi("multipart/alternative", _part("text/plain", ""), _part("text/html", html)),
    }


DATUM_CODE = "731904"
DATUM_PLAIN = (
    "Datum Recruitment - Candidate registration code\r\n\r\nHi Alex Example,\r\n\r\n"
    "To verify your email address and sign into the Candidate Portal, use this 6-digit "
    "code (you can copy and paste it):\r\n\r\n"
    f"Your code: {DATUM_CODE}\r\n\r\nThe code expires after 15 minutes.\r\n\r\n"
    "If you did not request a code, you can ignore this email.\r\n\r\n\r\n"
    "Thank you,\r\nDatum Talent Acquisition\r\n\r\n?\r\n\r\n"
    "Datum (UK) Limited 1 Example Park Way, Example City,\r\n\r\n"
    "EX1 2AB United Kingdom VAT number: GB000000000"
)


def portal_mail(from_hdr: str, plain: str = DATUM_PLAIN) -> dict:
    """The real Datum shape: multipart/mixed > alternative > plain + html."""
    html = "<html><body>" + plain.replace("\r\n", "<br>") + "</body></html>"
    return {
        "headers": {
            "From": from_hdr,
            "Subject": "Datum Careers - Candidate registration code",
            # Salesforce bounce address: the From address is encoded in it.
            "Return-Path": "<{}__abc@bnc.salesforce.com>".format(
                re.search(r"<?([^<>\s]+)@([^<>\s]+?)>?$", from_hdr).expand(r"\1=\2")
            ),
        },
        "payload": _multi(
            "multipart/mixed",
            _multi("multipart/alternative", _part("text/plain", plain), _part("text/html", html)),
        ),
    }


DATUM_FROM = "Datum Careers <careers@careers.datum.example>"
DATUM_PORTAL = "https://datumhr.my.salesforce-sites.com/careers/apply"


# --- fake Gmail -----------------------------------------------------------------


class FakeGmail:
    def __init__(self):
        self.msgs: list[dict] = []
        self.queries: list[str] = []

    def add(self, mail: dict, *, age_sec: float) -> None:
        received = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        payload = dict(mail["payload"])
        payload["headers"] = [{"name": k, "value": v} for k, v in mail["headers"].items()]
        self.msgs.append(
            {
                "id": f"m{len(self.msgs)}",
                "internalDate": str(int(received.timestamp() * 1000)),
                "payload": payload,
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
        """Gmail semantics for `from:x` (address domain or a subdomain) and `subject:"..."`."""
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


DICE_ASK = dict(sender="dice.com", subject_hint="Verify your email", company="Dice")
DATUM_ASK = dict(
    sender="salesforce-sites.com",
    subject_hint="Enter authentication code",
    company="Datum",
    employer_urls=(DATUM_PORTAL,),
)


def lookup(mod: ModuleType, inbox: FakeGmail, ask: dict, **kw):
    mod._gmail_service = lambda: inbox
    args = dict(ask, minutes=10)
    args.update(kw)
    return mod.find_code(**args)


def one(mod: ModuleType, mail: dict, ask: dict, **kw):
    g = FakeGmail()
    g.add(mail, age_sec=20)
    return lookup(mod, g, ask, **kw)


# --- cases ----------------------------------------------------------------------


def dice_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    out = []

    # 1. The 2026-10-24 inbox: the Dice mail plus a newer LinkedIn receipt.
    g = FakeGmail()
    g.add(dice_mail(_dice_html(DICE_LINK)), age_sec=40)
    g.add(
        {
            "headers": {"From": "LinkedIn <jobs-noreply@linkedin.com>", "Subject": "Your application was sent"},
            "payload": _part("text/plain", "Your application was sent to Coho\r\n"),
        },
        age_sec=5,
    )
    r = lookup(mod, g, DICE_ASK)
    out.append(("dice_verify_link_returned", DICE_LINK, r.magic_link))
    out.append(("dice_no_code_invented", None, r.code))
    out.append(("dice_found_by_sender_filtered_pass", True, "from:dice.com" in r.searched_query))

    # 2. Same mail, but the button points at another site: not handed over.
    r = one(mod, dice_mail(_dice_html("https://tracker.example.net/account/q7Zr2LmX9vKp4TnB8sWd?u=x")), DICE_ASK)
    out.append(("button_to_other_domain_refused", "", r.magic_link))

    # 3. A button with some other label to the sender's own site: no link.
    r = one(mod, dice_mail(_dice_html("https://www.dice.com/jobs/q7Zr2LmX9vKp4TnB8sWd", "View jobs")), DICE_ASK)
    out.append(("non_verify_button_ignored", "", r.magic_link))

    # 4. A Dice verify mail when the agent asked for another site: refused.
    r = one(mod, dice_mail(_dice_html(DICE_LINK)), dict(sender="greenhouse.io", subject_hint="", company="Coho"))
    out.append(("dice_link_refused_for_other_site", "", r.magic_link))

    # 5. Mail older than the OTP request: ignored.
    g = FakeGmail()
    g.add(dice_mail(_dice_html(DICE_LINK)), age_sec=200)
    r = lookup(mod, g, DICE_ASK, not_before=datetime.now(timezone.utc) - timedelta(seconds=60))
    out.append(("dice_mail_before_not_before_ignored", "", r.magic_link))
    return out


def portal_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    out = []

    # 1. The 2026-10-24 inbox: the code mail twice (the agent asked twice).
    g = FakeGmail()
    g.add(portal_mail(DATUM_FROM), age_sec=130)
    g.add(portal_mail(DATUM_FROM), age_sec=10)
    out.append(("datum_code_found_for_salesforce_sites_ask", DATUM_CODE, lookup(mod, g, DATUM_ASK).code))

    # 2. The same code mail on another employer's Salesforce Sites job: refused.
    r = one(mod, portal_mail(DATUM_FROM), dict(DATUM_ASK, company="Coho", employer_urls=()))
    out.append(("datum_code_refused_for_other_employer", None, r.code))

    # 3. A stranger's code mail on the Datum job: refused.
    stranger = portal_mail("Proseware Accounts <no-reply@proseware.example>", DATUM_PLAIN.replace("Datum", "Proseware"))
    out.append(("stranger_code_refused", None, one(mod, stranger, DATUM_ASK).code))

    # 4. Employer mail only counts for a white-label vendor ask: a third-party
    #    site the agent named (not a vendor) still refuses Datum's code.
    r = one(mod, portal_mail(DATUM_FROM), dict(DATUM_ASK, sender="jobrelay.com", employer_urls=()))
    out.append(("employer_mail_refused_for_non_vendor_ask", None, r.code))
    return out


def unit_cases(mod: ModuleType) -> list[tuple[str, object, object]]:
    html = _dice_html(DICE_LINK)
    blank_plain = _multi("multipart/alternative", _part("text/plain", " \r\n"), _part("text/html", html))
    real_plain = _multi(
        "multipart/alternative", _part("text/plain", "Your code is 482915"), _part("text/html", html)
    )
    return [
        ("blank_plain_part_falls_back_to_html", True, "verification link below" in mod._walk_body(blank_plain)),
        ("blank_plain_part_keep_html_returns_html", True, "href=" in mod._walk_body(blank_plain, keep_html=True)),
        ("real_plain_part_still_preferred", "Your code is 482915", mod._walk_body(real_plain)),
        (
            "html_entity_number_not_a_code",
            None,
            mod.extract_code(mod._walk_body(_multi("multipart/alternative", _part("text/html", "<p>If you didn&#8217;t sign up</p>"))), "Verify Candidate Email"),
        ),
        ("anchor_needs_sender", "", mod.extract_magic_link(html, "Dice.com - Verify Candidate Email")),
        ("anchor_with_sender", DICE_LINK, mod.extract_magic_link(html, "", sender="no-reply@dice.com")),
        (
            "anchor_label_confirm_your_email",
            "https://app.contoso.example/c/Abc123Def456",
            mod.extract_magic_link(
                '<a href="https://app.contoso.example/c/Abc123Def456">Confirm your email</a>',
                "",
                sender="Contoso <hello@contoso.example>",
            ),
        ),
        (
            "lookalike_domain_refused",
            "",
            mod.extract_magic_link(
                '<a href="https://www.notdice.com/account/q7Zr2LmX9vKp">Verify Email</a>',
                "",
                sender="no-reply@dice.com",
            ),
        ),
        (
            "salesforce_sites_is_a_vendor",
            True,
            mod.SiteHint.parse("salesforce-sites.com", "Enter authentication code").vendor_named,
        ),
    ]


def main() -> int:
    mod = _load(REPO / "module4" / "otp_lookup.py", "otp_lookup_html_link_under_test")
    mod._candidate_name_words = lambda: {"alex", "example"}
    failed = 0
    total = 0
    for title, rows in (
        ("Dice verify button (job 2201)", dice_cases(mod)),
        ("Salesforce Sites portal code (job 2089)", portal_cases(mod)),
        ("body and link units", unit_cases(mod)),
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
