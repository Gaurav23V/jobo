"""Offline tests for module4.otp_lookup.extract_code.

No network, no credentials. Bodies below are trimmed from the real shapes of
verification emails the apply agent hit on the 2026-09-01 run (Northwind /
Azure B2C, Adatum Workday, Shine, SuccessFactors, TurboHire) plus adversarial
cases that previously broke naive "first 6-digit number wins" extraction.

Run: uv run python scripts/test_otp_extraction.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4.otp_lookup import (  # noqa: E402
    NO_CODE_EXPLANATIONS,
    extract_code,
    extract_magic_link,
)

CASES: list[tuple[str, str, str, str | None]] = [
    # (label, subject, body, expected)
    (
        "azure_b2c_northwind",
        "Northwind account email verification code",
        "Thanks for verifying your account!\n\nYour code is: 748291\n\n"
        "Sincerely,\nNorthwind\n",
        "748291",
    ),
    (
        "workday_adatum",
        "Verify your email address",
        "Hello,\n\nPlease use the following verification code to continue "
        "creating your candidate account:\n\n   903117\n\n"
        "This code expires in 15 minutes.\nAdatum Careers 2026\n",
        "903117",
    ),
    (
        "code_in_subject_only",
        "195043 is your Shine.com verification code",
        "Hi Alex,\n\nUse the code sent to your registered email to continue.\n"
        "If you did not request this, ignore this message.\n",
        "195043",
    ),
    (
        "successfactors_styled_block",
        "SAP SuccessFactors - Registration",
        "<html><body><p>Your one-time passcode:</p>"
        "<table><tr><td><strong>44 12 09</strong></td></tr></table>"
        "<p>Valid for 10 minutes. Copyright 2026 SAP SE.</p></body></html>",
        "441209",
    ),
    (
        "turbohire_otp_prefix",
        "Sign in to TurboHire",
        "OTP: 620814\n\nDo not share this with anyone.\n",
        "620814",
    ),
    (
        "code_before_label",
        "Your verification",
        "581923 is your verification code for XY Careers. It expires shortly.\n",
        "581923",
    ),
    # --- alphanumeric mixed-case codes (Greenhouse) ---
    # Verbatim bodies from real mail received 2026-09-05. The extractor's
    # alpha branch was [A-Z0-9]{6,8} (uppercase only), so all five were
    # missed and four otherwise-complete applications reported "no email
    # arrived" while the mail sat in the inbox.
    (
        "greenhouse_mixed_case_code",
        "Security code for your application to Alderly",
        "Hi Alex,\n\nCopy and paste this code into the security code field "
        "on your application:\n\nkRNWpqzs\n\nAfter you enter the code, resubmit "
        "your application.\n\n\u00a9 2026 Greenhouse\n"
        "18 West 18th Street, 11th Floor, New York, NY 10011\n",
        "kRNWpqzs",
    ),
    (
        "greenhouse_code_leading_digit",
        "Security code for your application to Bramblecast, LLC",
        "Copy and paste this code into the security code field on your "
        "application:\n\n7K2qrst4\n\n\u00a9 2026 Greenhouse\n"
        "18 West 18th Street, 11th Floor, New York, NY 10011\n",
        "7K2qrst4",
    ),
    (
        "greenhouse_code_all_digits_and_caps",
        "Security code for your application to Alderly",
        "Copy and paste this code into the security code field on your "
        "application:\n\n48m6TZ9H\n\n\u00a9 2026 Greenhouse\n",
        "48m6TZ9H",
    ),
    (
        # The company name sits in the subject line and is CamelCase, which
        # matches the widened code shape. It must not win over the real code.
        "company_name_in_subject_is_not_a_code",
        "Security code for your application to CodeHarbor",
        "Copy and paste this code into the security code field on your "
        "application:\n\nppqZvM4n\n\n\u00a9 2026 Greenhouse\n",
        "ppqZvM4n",
    ),
    # A brand that is NOT plain CamelCase ("Mongo"+"DB", "Open"+"AI",
    # "dbt"+"Labs") passes the word screen. Job 1266 (2026-10-04) typed
    # "Eastlake" into Greenhouse's 8-character code field and never recovered.
    (
        "brand_with_acronym_in_subject_is_not_a_code",
        "Security code for your application to Eastlake",
        "Copy and paste this code into the security code field on your "
        "application:\n\nppqZvM4n\n\n© 2026 Greenhouse\n",
        "ppqZvM4n",
    ),
    (
        "brand_openai_in_subject_is_not_a_code",
        "Security code for your application to OpenAI",
        "Copy and paste this code into the security code field on your "
        "application:\n\n7K2qrst4\n\n© 2026 Greenhouse\n",
        "7K2qrst4",
    ),
    (
        # Hardest shape: the real code is letters-only too. It is alone on
        # its own line; the brand is not.
        "brand_in_subject_letters_only_code",
        "Security code for your application to Eastlake",
        "Copy and paste this code into the security code field on your "
        "application:\n\nkRNWpqzs\n\nAfter you enter the code, resubmit "
        "your application.\n\n© 2026 Greenhouse\n",
        "kRNWpqzs",
    ),
    (
        "brand_lowercase_prefix_in_subject",
        "Security code for your application to dbtLabs",
        "Copy and paste this code into the security code field on your "
        "application:\n\nR3hTKGwN\n\n© 2026 Greenhouse\n",
        "R3hTKGwN",
    ),
    # --- adversarial: things that look like codes but aren't ---
    (
        # The Greenhouse footer address ends in a 5-digit zip. Before the
        # negative-context screen the loose pass returned 10011 as the code.
        "reject_footer_zip_code",
        "Security code for your application to Bluegum",
        "Copy and paste this code into the security code field on your "
        "application:\n\nR3hTKGwN\n\n\u00a9 2026 Greenhouse\n"
        "18 West 18th Street, 11th Floor, New York, NY 10011\n",
        "R3hTKGwN",
    ),
    (
        "reject_year_prefer_code",
        "Verify your email",
        "Copyright 2026 Kestrel. All rights reserved.\n"
        "Your security code is 337914.\nRequisition 2024 open roles.\n",
        "337914",
    ),
    (
        "reject_phone_number",
        "Confirm your application",
        "Questions? Call us at 8005551234.\n"
        "Your confirmation code is 771205.\n",
        "771205",
    ),
    (
        "no_code_present",
        "Application received",
        "Thank you for applying to Lucerne. We will review your application "
        "and be in touch. Reference 2026 hiring season.\n",
        None,
    ),
    (
        "alphanumeric_code",
        "Your access code",
        "Please enter this access code to continue: 7F3K9Q\n",
        "7F3K9Q",
    ),
    (
        "four_digit_pin",
        "Your PIN",
        "Your PIN is 4471. Enter it on the verification screen.\n",
        "4471",
    ),
    (
        "html_table_split_digits",
        "Verification code",
        "<div>Your verification code is</div>"
        "<table><tr><td>3</td><td>1</td><td>4</td><td>1</td><td>5</td><td>9</td></tr></table>",
        "314159",
    ),
    (
        "empty_input",
        "",
        "",
        None,
    ),
]

# Cases that must be REJECTED when no sender filter matched and we therefore
# demand explicit verification wording (require_context=True). This is the
# guard that lets the unfiltered fallback search be safe.
CONTEXT_STRICT_CASES: list[tuple[str, str, str, str | None]] = [
    (
        # Same as the job 1266 shape, but on the unfiltered-sender path.
        "strict_brand_in_subject_letters_only_code",
        "Security code for your application to Eastlake",
        "Copy and paste this code into the security code field on your "
        "application:\n\nkRNWpqzs\n\n© 2026 Greenhouse\n",
        "kRNWpqzs",
    ),
    (
        "strict_accepts_labelled_code",
        "Verify your email address",
        "Your verification code is 553311. Expires in 10 minutes.\n",
        "553311",
    ),
    (
        "strict_rejects_bare_number",
        "Your order has shipped",
        "Tracking number 884213 will update shortly. Thanks for shopping.\n",
        None,
    ),
    (
        "strict_rejects_standalone_line",
        "Meeting notes",
        "Action items below.\n\n449201\n\nThat is the room number.\n",
        None,
    ),
    (
        "strict_accepts_otp_prefix",
        "Sign in",
        "OTP: 771034\nDo not share this code.\n",
        "771034",
    ),
    (
        # Job 2069, 2026-10-22 (micro1 / jobs.micro1.ai), the mail's real
        # shape: the brand follows "verification code" in the subject, and
        # "micro1" was handed to the agent three times.
        "strict_micro1_brand_after_label",
        "Your 6-digit verification code from micro1",
        "Hello User,\n\nYour one-time verification code is: 482913.\n\n"
        "Best wishes,\nmicro1 team\n",
        "482913",
    ),
]

# (label, from_header, subject, body, expected). The sender's own name is
# never the code, even when the mail does not state the code's shape.
SENDER_CASES: list[tuple[str, str, str, str, str | None]] = [
    (
        "sender_brand_not_a_code",
        "micro1 <support@micro1.ai>",
        "Your verification code from micro1",
        "Hello User,\n\nYour one-time verification code is: 482913.\n",
        "482913",
    ),
    (
        "sender_brand_only_token_gives_none",
        "micro1 <support@micro1.ai>",
        "Your verification code from micro1",
        "Hello User,\n\nYour code is shown in the image below.\n",
        None,
    ),
    (
        # Shape stated: a mixed token of the wrong shape is refused even
        # without the sender.
        "stated_shape_refuses_other_shapes",
        "",
        "Your 6-digit code from acme7",
        "Thanks for signing up with acme7.\n",
        None,
    ),
    (
        # A password rule in the same mail is not the code's shape.
        "password_rule_is_not_code_shape",
        "Careers <no-reply@careers.example.com>",
        "Verify your email",
        "Your verification code is 553311.\n"
        "Your password must be at least 8 characters long.\n",
        "553311",
    ),
    (
        "eight_character_code_shape",
        "Greenhouse <no-reply@greenhouse.io>",
        "Security code for your application",
        "Copy this 8-character security code into your application:\n\nkRNWpqzs\n",
        "kRNWpqzs",
    ),
]

# Real bodies that arrive INSTEAD of a code. These must NOT yield a code, and
# must be recognised as blockers so the pipeline stops asking. The first is
# the verbatim Northwind message that burned 3 OTP rounds x 5 jobs on the
# 2026-09-02 run: an earlier job had registered the address, so every later
# signup got this instead of a code.
BLOCKER_CASES: list[tuple[str, str, str, bool]] = [
    (
        "northwind_account_exists",
        "Verification Code - Northwind",
        "*************************\r\nVerify your email address\r\n"
        "*************************\r\n\r\nThere was an attempt to sign up using this "
        "email address but there is already an account connected to it.\r\n"
        'Please go to the Sign In page and select "Forgot Password?" to reset your '
        "password, or use a different email address to sign up.\r\n\r\nSincerely,\r\n"
        "Northwind\r\n",
        True,
    ),
    (
        "rate_limited",
        "Too many attempts",
        "You have made too many requests for a verification code. Please try again later.\n",
        True,
    ),
    (
        "account_locked",
        "Account notice",
        "Your account has been locked after multiple failed sign-in attempts.\n",
        True,
    ),
    (
        "genuine_code_is_not_a_blocker",
        "Verify your email address",
        "Your verification code is 553311. Expires in 10 minutes.\n",
        False,
    ),
]

# (label, subject, body, expected_link_or_empty)
# Portals that mail a one-click sign-in URL instead of a code. Before these
# were handled the OTP path reported "no email arrived" while two perfectly
# good auth links sat in the inbox (job651, Goodfit, 2026-09-05).
MAGIC_LINK_CASES = [
    (
        "goodfit_auth_link",
        "Goodfit authentication link: 5 Sept, 10:05 pm IST",
        "Sign in to Goodfit\r\n\r\nClick the button below to securely sign in to "
        "your account.\r\n\r\nSign in to Goodfit\r\n"
        "https://jobs.goodfit.so/api/auth/magic-link/verify?"
        "token=ExampleMagicLinkToken0000000000&callbackURL=%2Fjobs%2Fapply\r\n",
        "https://jobs.goodfit.so/api/auth/magic-link/verify?"
        "token=ExampleMagicLinkToken0000000000&callbackURL=%2Fjobs%2Fapply",
    ),
    (
        # A code mail must NOT be mistaken for a link mail, or the agent would
        # go navigating instead of typing the code it already has.
        "greenhouse_code_mail_has_no_link",
        "Security code for your application to Alderly",
        "Copy and paste this code into the security code field on your "
        "application:\n\nkRNWpqzs\n\nhttps://my.greenhouse.io/applications\n",
        "",
    ),
    (
        # Marketing mail with a tracking URL must not look like an auth link.
        "tracking_url_is_not_an_auth_link",
        "Your weekly job digest",
        "Here are this week's roles.\n"
        "https://track.example.com/click?token=abcdefghijklmnopqrstuvwxyz123\n",
        "",
    ),
]


def main() -> int:
    passed, failed = 0, []
    total = (
        len(CASES)
        + len(CONTEXT_STRICT_CASES)
        + len(SENDER_CASES)
        + len(BLOCKER_CASES)
        + len(MAGIC_LINK_CASES)
    )
    for label, subject, body, expected in CASES:
        got = extract_code(body, subject=subject)
        if got == expected:
            passed += 1
            print(f"  PASS  {label:28s} -> {got!r}")
        else:
            failed.append((label, expected, got))
            print(f"  FAIL  {label:28s} expected {expected!r}, got {got!r}")

    print("\n  -- require_context=True (unfiltered fallback safety) --")
    for label, subject, body, expected in CONTEXT_STRICT_CASES:
        got = extract_code(body, subject=subject, require_context=True)
        if got == expected:
            passed += 1
            print(f"  PASS  {label:28s} -> {got!r}")
        else:
            failed.append((label, expected, got))
            print(f"  FAIL  {label:28s} expected {expected!r}, got {got!r}")

    print("\n  -- sender's own name and the stated code shape (job 2069) --")
    for label, sender, subject, body, expected in SENDER_CASES:
        got = extract_code(body, subject=subject, require_context=True, sender=sender)
        loose = extract_code(body, subject=subject, sender=sender)
        if got == expected and loose == expected:
            passed += 1
            print(f"  PASS  {label:28s} -> {got!r}")
        else:
            failed.append((label, expected, (got, loose)))
            print(f"  FAIL  {label:28s} expected {expected!r}, got strict={got!r} loose={loose!r}")

    print("\n  -- blocker detection (mail that arrives INSTEAD of a code) --")
    for label, subject, body, expect_blocker in BLOCKER_CASES:
        haystack = f"{subject}\n{body}".lower()
        matched = any(re.search(pat, haystack) for pat, _ in NO_CODE_EXPLANATIONS)
        code = extract_code(body, subject=subject)
        # A blocker case must be recognised AND must not produce a code.
        good = (matched == expect_blocker) and (not expect_blocker or code is None)
        if good:
            passed += 1
            print(f"  PASS  {label:28s} blocker={matched} code={code!r}")
        else:
            failed.append((label, f"blocker={expect_blocker}", f"blocker={matched} code={code!r}"))
            print(
                f"  FAIL  {label:28s} expected blocker={expect_blocker}, "
                f"got blocker={matched} code={code!r}"
            )

    print("\n  -- magic sign-in links (portals that send a URL, not a code) --")
    for label, subject, body, expected in MAGIC_LINK_CASES:
        got = extract_magic_link(body, subject=subject)
        if got == expected:
            passed += 1
            shown = (got[:52] + "...") if len(got) > 55 else (got or "(none)")
            print(f"  PASS  {label:34s} -> {shown}")
        else:
            failed.append((label, expected, got))
            print(f"  FAIL  {label:34s} expected {expected!r}, got {got!r}")

    print(f"\n{passed}/{total} passed")
    if failed:
        print("\nFailures:")
        for label, exp, got in failed:
            print(f"  {label}: expected {exp!r}, got {got!r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
