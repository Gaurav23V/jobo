"""Offline replay: a Taleo access code with punctuation in it (GHO-108).

Job 3435, 2026-10-30: after sign-in, a Taleo tenant asked for an emailed
access code. The agent asked `NEED_OTP: taleo.net | Access Code`. The mail
arrived 9 seconds before the request, as
    From: "Talent Acquisition (Do Not Reply)" <careers@recruiting.<employer>.com>
          (relayed by a taleo.net host, so `from:taleo.net` misses it)
    Subject: Multifactor authentication access code
    text/plain: "... use the following access code ...\n\n Access Code: \n <code> \n"
and the code was 10 printable characters with punctuation in it. The
unfiltered pass listed the mail, the employer check passed it, and then
extract_code found nothing, because a code was only ever letters and
digits. No log line said so; the agent reported that no code arrived.

Employer, domains and codes are made up. No network, no Gmail, no browser.

Run through scripts/run_regression_tests.sh (not directly).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import module4.otp_lookup as otp  # noqa: E402
from test_otp_spam_vendor_domain import FakeGmail  # noqa: E402

CODE = "Tz#=kQ4?Wm"
SENDER = '"Talent Acquisition (Do Not Reply)" <careers@recruiting.fbkhealth.com>'
SUBJECT = "Multifactor authentication access code"


def taleo_body(code: str) -> str:
    """The real mail's plain part: blank lines, then the code on its own line."""
    return (
        " \n" * 10
        + " To better secure your account with Fabrikam Health, multifactor "
        "authentication is enabled. Please use the following access code to log in "
        "to your account. \n \n Access Code: \n "
        + code
        + " \n \n Thank You, \n Fabrikam Health Talent Acquisition \n \n \n \n"
        " Please do not reply to this email. Replies are undeliverable."
    )


def extraction_cases() -> list[tuple[str, object, object]]:
    def ex(body, subject=SUBJECT, strict=True, sender=SENDER):
        return otp.extract_code(body, subject=subject, require_context=strict, sender=sender)

    html = (
        "<html><body><p>Please use the following access code to log in.</p>"
        f"<p>Access Code:</p><p><b>{CODE.replace('&', '&amp;')}</b></p>"
        "<p>Thank You,</p></body></html>"
    )
    return [
        ("taleo_code_strict", CODE, ex(taleo_body(CODE))),
        ("taleo_code_loose", CODE, ex(taleo_body(CODE), strict=False)),
        ("taleo_code_html_one_line", CODE, ex(html)),
        ("code_with_ampersand", "Rb&7xK!q2P", ex(taleo_body("Rb&7xK!q2P"))),
        # A slice of a symbol code is never returned instead of the code.
        ("no_alnum_slice", "Ab12cd=xY", ex(taleo_body("Ab12cd=xY"))),
        # Plain codes keep their old reading, trailing full stop or not.
        ("plain_digits_unchanged", "482913", ex("Your access code: 482913.")),
        ("plain_mixed_unchanged", "kRNWpqzs", ex("Security code:\n kRNWpqzs\n")),
        # Shapes that are not codes, even after a code label.
        ("date_not_a_code", None, ex("Verification code: 10/30/2026 was requested.")),
        ("phone_not_a_code", None, ex("Access code: +1-555-0100-22 (call us)")),
        ("email_not_a_code", None, ex("Login code: ops-team@fabrikam.example sent")),
        ("url_not_a_code", None, ex("Access code: www.fabrikam.example/x?a=1 here")),
        ("masked_not_a_code", None, ex("Access code: ****-**** will expire")),
        # A password line is not a code label.
        ("password_label_ignored", None, ex("Your temporary password: Ab#12cd!x", subject="Welcome")),
        # The mail's stated shape still rules.
        (
            "demanded_shape_respected",
            None,
            ex("Your 6-digit access code:\n Tz#=kQ4?Wm\n"),
        ),
    ]


ASK = dict(
    sender="taleo.net",
    subject_hint="Access Code",
    company="Fabrikam Health",
    employer_urls=("https://fbkhealth.taleo.net/careersection/10000/jobapply.ftl",),
)
NOISE = [
    {"from": "LinkedIn <jobs-noreply@linkedin.com>", "subject": "Your application was sent to Coho",
     "body": "Your application was sent to Coho.\n"},
    {"from": "noreply@mail.contoso.jobs", "subject": "Thank you for applying to Contoso!",
     "body": "Thanks for applying. Reference 482915.\n"},
]


def lookup(inbox: FakeGmail, **kw):
    otp._gmail_service = lambda: inbox
    args = dict(ASK, minutes=10)
    args.update(kw)
    return otp.find_code(**args)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def mailbox_cases() -> list[tuple[str, object, object]]:
    out = []

    # 1. The 2026-10-30 inbox: code mail from the employer's own domain,
    #    newer job-board mail above it.
    g = FakeGmail()
    g.add({"from": SENDER, "subject": SUBJECT, "body": taleo_body(CODE)}, age_sec=20)
    for i, n in enumerate(NOISE):
        g.add(n, age_sec=5 + i)
    r = lookup(g)
    out.append(("taleo_request_finds_employer_mail", CODE, r.code))
    out.append(("found_by_unfiltered_pass", "newer_than:1d", r.searched_query))

    # 2. Another Taleo tenant's access code is never this job's code.
    g = FakeGmail()
    other = taleo_body(CODE).replace("Fabrikam Health", "Northwind Traders")
    g.add({"from": '"Talent Acquisition" <careers@recruiting.northwind.example>',
           "subject": SUBJECT, "body": other}, age_sec=20)
    out.append(("other_tenant_code_refused", None, lookup(g).code))

    # 3. A mail that passes the sender check but holds no code says so in
    #    the log (job 3435's mail was read on every poll without a trace).
    g = FakeGmail()
    g.add({"from": SENDER, "subject": "Your Fabrikam Health account",
           "body": "Welcome back to Fabrikam Health careers.\n"}, age_sec=20)
    cap = _Capture()
    otp.logger.addHandler(cap)
    otp.logger.setLevel(logging.INFO)
    try:
        r = lookup(g)
    finally:
        otp.logger.removeHandler(cap)
    out.append(("no_code_mail_found_nothing", None, r.code))
    out.append((
        "no_code_mail_logged",
        True,
        any("found no code in it" in line and "fbkhealth" in line for line in cap.lines),
    ))
    return out


def main() -> int:
    cases = extraction_cases() + mailbox_cases()
    passed = 0
    for label, expected, got in cases:
        ok = got == expected
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}: expected={expected!r} got={got!r}")
    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
