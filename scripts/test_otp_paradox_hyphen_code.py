"""Offline tests: Paradox's hyphenated "<code> is your ... code" mail (GHO-96).

Job 2244, 2026-10-25: Paradox's Olivia mails the code as two hyphenated halves
placed 60+ characters before its label ("418-207 is your Contoso Corporation
AMEA Candidate Data verification code"), and every one of its mails ends
with the employer's street address. extract_code found no labelled code and
its loose fallback returned the footer ZIP, a 5-digit number, which was typed
into the 6-digit field on both attempts.

No network, no credentials. Bodies keep the real mail's shape with the
employer and address replaced.

Run: uv run python scripts/test_otp_paradox_hyphen_code.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4.otp_lookup import extract_code  # noqa: E402

SENDER = "Olivia from Contoso <contoso@paradox.ai>"
FOOTER = (
    "--- | If you no longer want to receive these alerts, you can "
    "[manage preferences](https://example.paradox.ai/p) or "
    "[unsubscribe](https://example.paradox.ai/u) Contoso Corporate Headquarters "
    "1200 E Main Rd, Springfield, Ohio 45502, United States ---"
)
CODE_SUBJECT = "Your Contoso Corporation AMEA Verification Code"
CODE_BODY = (
    "| --- | | Hi Alex, 418-207 is your Contoso Corporation AMEA Candidate Data "
    "verification code. For security, please enter this code on the two-step "
    "verification screen or within your text thread to verify your identity. "
    "This code will expire in an hour and can only be used once. " + FOOTER
)
INVITE_SUBJECT = "Complete Your Contoso Corporation AMEA Full Stack Developer I Application"
INVITE_BODY = (
    "| --- | | Complete Your Contoso Corporation AMEA Full Stack Developer I "
    "Application --- | Hello Alex! Please complete your application for the "
    "Contoso Corporation AMEA Full Stack Developer I position within 48 hours: "
    "https://example.paradox.ai/c [ Get Started ](https://example.paradox.ai/c) "
    + FOOTER
)

# (label, subject, body, require_context, expected)
CASES: list[tuple[str, str, str, bool, str | None]] = [
    ("paradox_code_mail_loose", CODE_SUBJECT, CODE_BODY, False, "418207"),
    # Strict mode is the first pass find_code runs on a sender-filtered mail.
    ("paradox_code_mail_strict", CODE_SUBJECT, CODE_BODY, True, "418207"),
    # The invite arrives seconds before the code mail. Its only number-shaped
    # token is the footer ZIP, which must never be handed over as a code.
    ("paradox_invite_has_no_code", INVITE_SUBJECT, INVITE_BODY, False, None),
    (
        "address_zip_without_code_wording",
        "Welcome",
        "Thanks for applying.\nAcme Inc, 18 Harbor Street, Portland, Maine 04101\n",
        False,
        None,
    ),
    (
        "hyphen_code_after_label",
        "Sign-in",
        "Your verification code is 5821-0937. It expires in 10 minutes.",
        False,
        "58210937",
    ),
    ("lead_plain_digits_still_work", "", "731905 is your login code.", True, "731905"),
    (
        "lead_mixed_token",
        "",
        "Hi, Qx7Lm2 is your Acme Careers one-time passcode.",
        True,
        "Qx7Lm2",
    ),
    # "This is your code" must not return the word "This".
    ("lead_word_is_not_a_code", "", "This is your verification code: 662014", True, "662014"),
    # A phone tail next to code wording is not two code halves.
    (
        "phone_tail_not_joined",
        "",
        "Your verification code is 449120. Questions? Call 555-1234.",
        False,
        "449120",
    ),
    # Dates next to code wording are not joined into codes.
    (
        "date_not_joined",
        "",
        "Verification code requested on 10-25-2026 and 2026-10-25.\n\n  730118\n",
        False,
        "730118",
    ),
]


def main() -> int:
    passed = 0
    total = len(CASES)
    for label, subject, body, strict, expected in CASES:
        got = extract_code(body, subject=subject, require_context=strict, sender=SENDER)
        ok = got == expected
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}: expected={expected!r} got={got!r}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
