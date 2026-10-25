"""Offline replay of the hirist.tech mails that arrived but read as no mail.

Job 2330 (Antino, via hirist.tech), 2026-10-25 10:51-10:55 IST: the agent asked
`NEED_PASSWORD_RESET: hirist.tech`, then twice `NEED_OTP: hirist.tech`. All
three mails landed within seconds:
    From: hirist.tech <info@hirist.tech>
    Subject: hirist.tech - Reset Password / Your OTP for login - hirist.tech
    multipart/alternative: text/plain "Please Enable HTML" (18 bytes) + the
    real text/html part (the OTP, or an UNQUOTED <a href=...>click here</a>)
_walk_body preferred the non-blank plain part, so every mail read as "Please
Enable HTML": no digits, no link. Sibling of the GHO-91 blank-part fix.

Also: the candidate's long-standing hirist account signs in by emailed OTP and
must never be password-reset (as Internshala on GHO-60), so the driver refuses
NEED_PASSWORD_RESET for it and the prompt says to use the OTP login.

Fixtures keep the real MIME shapes; names, codes and tokens are replaced.
No network, no Gmail, no browser: Gmail is a fake service.

Run through scripts/run_regression_tests.sh (not directly).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from module4 import agent_driver, otp_lookup, password_reset, prompts  # noqa: E402
from test_otp_html_link_and_portal import FakeGmail, _multi, _part  # noqa: E402

# --- fixtures -----------------------------------------------------------------

PLACEHOLDER = "Please Enable HTML"
HIRIST_FROM = "hirist.tech <info@hirist.tech>"
OTP_CODE = "529148"
OTP_CODE_2 = "806137"
RESET_LINK = (
    "https://postoffice.hirist.tech/CL0/https:%2F%2Fwww.hirist.tech%2Freset-password%2F"
    "q7Zr2LmX9vKp4TnB8sWd$2b$10$AbCdEfGhIjKlMnOpQrStUv%2F..wXyZ0123/1/"
    "0100019a1b2c3d4e-aaaa-bbbb/Zx9Yw8Vu7Ts6Rq5Po4Nm=452"
)


def _otp_html(code: str) -> str:
    """The real hirist OTP mail: styled table, code in a preheader and a box."""
    store = "https://postoffice.hirist.tech/CL0/https:%2F%2Fplay.google.com%2Fstore/1/0100019a/Qw1Er2=452"
    return (
        "<!doctype html><html><head><title>Verification OTP – hirist</title>"
        "<style>table {border-collapse:collapse;} @media only screen and (max-width:620px) "
        "{ .container { width:100% !important; } .otp-code { font-size: 28px !important; "
        "letter-spacing: 6px !important; } }</style></head><body>"
        f"<div style=\"display:none\">Your OTP for login is {code}. This code is valid for "
        "10 minutes. Do not share it with anyone.</div>"
        f"<table class=\"container\"><tr><td>Get the app: <a href=\"{store}\">Android</a> | "
        f"<a href=\"{store}\">iOS</a></td></tr>"
        "<tr><td class=\"px\"><p>Hi,</p><p>We received a request to log in to your account. "
        "Use the One-Time Password (OTP) below to complete your login:</p>"
        f"<p>YOUR ONE-TIME PASSWORD</p><div class=\"otp-code\">{code}</div>"
        "<p>Valid for 10 minutes</p><p><b>Security Note:</b> Never share this OTP with "
        "anyone. Our support team will never ask for your verification code.</p>"
        "<p>If you did not attempt to log in, please ignore this email.</p></td></tr>"
        "<tr><td>You are receiving this email because you are registered to hirist.tech as "
        "a Jobseeker. Copyright &copy; 2026 hirist . All rights reserved. Sent by "
        "hirist.tech | B-0, Sector 000, Example City - 110000.</td></tr></table></body></html>"
    )


RESET_HTML = (
    "Dear Alex Example,<br><br>\n"
    f"It seems you have forgotten your password. Please <a href={RESET_LINK}>click here</a> "
    "to reset your password.\nYour new password must be at least 6 characters long.\n"
    "Please note that this link will expire in 24 hours.<br><br> Do let us know if you "
    "face any problem in resetting your password<br><br>\nBest Regards,<br>\n"
    "Team hirist.tech<br>hello@hirist.tech\n"
    '<img alt="" src="http://postoffice.hirist.tech/CI0/0100019a/Lk3Jh2" '
    'style="display: none; width: 1px; height: 1px;">'
)


def hirist_mail(subject: str, html: str, plain: str = PLACEHOLDER) -> dict:
    return {
        "headers": {"From": HIRIST_FROM, "Subject": subject},
        "payload": _multi("multipart/alternative", _part("text/plain", plain), _part("text/html", html)),
    }


OTP_SUBJECT = "Your OTP for login - hirist.tech"
RESET_SUBJECT = "hirist.tech - Reset Password"


def inbox(*mails: tuple[dict, float]) -> FakeGmail:
    g = FakeGmail()
    for mail, age in mails:
        g.add(mail, age_sec=age)
    return g


# --- cases ----------------------------------------------------------------------


def lookup_cases() -> list[tuple[str, object, object]]:
    out = []
    # 1. The 2026-10-25 inbox: reset mail, then two OTP mails, newest last.
    g = inbox(
        (hirist_mail(RESET_SUBJECT, RESET_HTML), 220),
        (hirist_mail(OTP_SUBJECT, _otp_html(OTP_CODE)), 120),
        (hirist_mail(OTP_SUBJECT, _otp_html(OTP_CODE_2)), 5),
    )
    otp_lookup._gmail_service = lambda: g
    r = otp_lookup.find_code(sender="hirist.tech", subject_hint="", minutes=10, company="Antino")
    out.append(("hirist_otp_found_newest", OTP_CODE_2, r.code))

    g = inbox((hirist_mail(OTP_SUBJECT, _otp_html(OTP_CODE)), 20))
    otp_lookup._gmail_service = lambda: g
    r = otp_lookup.find_code(sender="hirist.tech", subject_hint="", minutes=10, company="Antino")
    out.append(("hirist_otp_found_single", OTP_CODE, r.code))

    # 2. The reset mail: the link sits in an unquoted href.
    g = inbox((hirist_mail(RESET_SUBJECT, RESET_HTML), 20))
    otp_lookup._gmail_service = lambda: g
    r = password_reset.find_reset_link(domain="hirist.tech")
    out.append(("hirist_reset_link_found", RESET_LINK, r.url))
    return out


def unit_cases() -> list[tuple[str, object, object]]:
    def body(plain: str, html: str = "<p>Your code is <b>482915</b></p>", **kw) -> str:
        return otp_lookup._walk_body(
            _multi("multipart/alternative", _part("text/plain", plain), _part("text/html", html)), **kw
        )

    return [
        ("placeholder_plain_falls_back_to_html", True, "482915" in body(PLACEHOLDER)),
        ("placeholder_keep_html_returns_html", True, "<b>" in body(PLACEHOLDER, keep_html=True)),
        ("view_in_browser_stub_skipped", True, "482915" in body("View this email in your browser.\r\n")),
        (
            "html_client_stub_skipped",
            True,
            "482915" in body("This email requires an HTML-capable email client."),
        ),
        ("real_plain_still_preferred", "Your code is 731904", body("Your code is 731904")),
        (
            "long_plain_mentioning_html_kept",
            True,
            "731904" in body("Your code is 731904. " + "Thanks for joining. " * 12 + "Enable HTML for images."),
        ),
        (
            "placeholder_without_html_part_kept",
            PLACEHOLDER,
            otp_lookup._walk_body(_multi("multipart/alternative", _part("text/plain", PLACEHOLDER))),
        ),
        ("anchor_double_quoted", True, "https://a.example/r/1" in password_reset.mail_text('<a href="https://a.example/r/1">x</a>')),
        ("anchor_unquoted", True, "https://a.example/r/1" in password_reset.mail_text("<a href=https://a.example/r/1>x</a>")),
    ]


def reset_policy_cases() -> list[tuple[str, object, object]]:
    # Without the refusal a request would wait for the mail and search the
    # inbox: keep that fast and offline.
    otp_lookup._gmail_service = lambda: FakeGmail()
    agent_driver.PASSWORD_RESET_WAIT_SEC = 0

    def ask(domain: str) -> str:
        return agent_driver._resolve_password_reset(f"NEED_PASSWORD_RESET: {domain}", job_id=2330)

    msg = ask("hirist.tech")
    return [
        ("hirist_reset_refused", True, msg.startswith("PASSWORD_RESET_REFUSED")),
        ("hirist_refusal_points_at_otp", True, "NEED_OTP: hirist.tech" in msg),
        ("www_hirist_refused", True, ask("www.hirist.tech").startswith("PASSWORD_RESET_REFUSED")),
        ("hirist_com_refused", True, ask("hirist.com").startswith("PASSWORD_RESET_REFUSED")),
        ("internshala_refused", True, ask("internshala.com").startswith("PASSWORD_RESET_REFUSED")),
        ("ats_domain_not_otp_site", "", password_reset.otp_signin_domain("hazelton.com")),
        ("lookalike_not_otp_site", "", password_reset.otp_signin_domain("nothirist.tech")),
        (
            "prompt_says_otp_not_reset_for_hirist",
            True,
            "hirist.tech" in prompts.SYSTEM_PROMPT.split("## Locked-out accounts")[1].split("When you hit")[0],
        ),
    ]


def retry_notice_cases() -> list[tuple[str, object, object]]:
    # The stored statuses of 2330's first two attempts; each retry read the
    # quoted failure as a dead wall and stopped at the sign-in page without
    # asking for a code.
    missed = (
        "hirist.tech requires email verification to sign in (account exists, password unknown) "
        "but no OTP or password-reset email arrived after 3 send attempts (1 reset + 2 OTP resends)"
    )
    missed_again = (
        'LinkedIn "Apply on company website" routes to hirist.tech, which requires login; a prior '
        "attempt already exhausted 3 OTP/reset email attempts with no mail arriving for "
        "alex@example.com, and hirist.tech is excluded from the password-reset flow (OTP-only sign-in)"
    )

    def retry(previous: str | None) -> str:
        return prompts.user_prompt(
            job_id=2330, company="Acme", title="Dev", url="https://www.hirist.tech/j/1",
            resume_path="/tmp/r.pdf", cover_path=None, previous_failure=previous,
        )

    return [
        ("missed_otp_retry_asks_for_code_again", True, "end\nyour turn with NEED_OTP: <that site>" in retry(missed)),
        ("missed_otp_second_retry_too", True, "NEED_OTP: <that site>" in retry(missed_again)),
        ("missed_otp_not_called_unsolvable", False, "unsolvable" in retry(missed_again)),
        ("missed_otp_old_failure_not_quoted", False, "exhausted" in retry(missed_again)),
        ("other_failure_still_quoted", True, "no file input" in retry("resume upload failed: no file input")),
        ("missed_code_not_found_too", True, "NEED_OTP" in retry("verification code not found in the inbox")),
        ("upload_failure_no_otp_note", False, "NEED_OTP" in retry("resume upload failed: no file input")),
        ("captcha_failure_no_otp_note", False, "NEED_OTP" in retry("captcha wall on apply page")),
        ("first_attempt_no_otp_note", False, "NEED_OTP" in retry(None)),
    ]


def main() -> int:
    otp_lookup._candidate_name_words = lambda: {"alex", "example"}
    real_service = otp_lookup._gmail_service
    failed = 0
    total = 0
    try:
        for title, rows in (
            ("hirist.tech mails (job 2330)", lookup_cases()),
            ("placeholder plain parts and unquoted links", unit_cases()),
            ("no password reset on OTP sign-in sites", reset_policy_cases()),
            ("retry after a missed emailed code", retry_notice_cases()),
        ):
            print(f"\n  -- {title} --")
            for label, expected, got in rows:
                ok = got == expected
                total += 1
                failed += not ok
                print(f"  {'PASS' if ok else 'FAIL'}  {label:50s} expected={expected!r} got={got!r}")
    finally:
        otp_lookup._gmail_service = real_service
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
