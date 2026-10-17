#!/usr/bin/env python3
"""Test the password-reset link extractor (offline, no Gmail needed).

The risky part of the NEED_PASSWORD_RESET handoff is picking the RIGHT url out
of an email full of them (logo, unsubscribe, privacy, help centre, social
icons). Handing the agent a tracking link wastes a round; handing it a reset
link for a DIFFERENT service would be a security problem.

Bodies below are modelled on the real transactional mail these portals send.

Run: uv run python scripts/test_password_reset.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import password_reset as pr  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:50s} {note}")


SUCCESSFACTORS = """
Dear Candidate,

We received a request to reset the password for your account.

Please click the link below to set a new password:
https://career5.successfactors.eu/careers/resetPassword?token=0123456789abcdef01234567&company=hazelton

If you did not request this, ignore this email.

Privacy Policy: https://www.sap.com/about/legal/privacy.html
Unsubscribe: https://career5.successfactors.eu/unsubscribe?u=99
[image: logo] https://career5.successfactors.eu/assets/logo.png
"""

WORKDAY = """
Hello,

To reset your Workday account password, use the link below. This link expires
in 24 hours.

Reset your password:
https://adatum.wd3.myworkdayjobs.com/reset-password/abc123DEF456ghi789JKL

Need help? https://adatum.wd3.myworkdayjobs.com/help
Follow us: https://twitter.com/adatum
"""

NOISY_FIRST_LINK = """
[image] https://cdn.rippling.com/logo.png
View this email in your browser: https://ats.rippling.com/email/view/9931

Hi Alex,

Someone requested a password reset. Click here to choose a new password:
https://ats.rippling.com/account/set-password?t=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5

Unsubscribe https://ats.rippling.com/unsubscribe
Terms https://ats.rippling.com/terms
"""

NO_RESET_LINK = """
Hi Alex, thanks for applying. We'll be in touch.
Visit us: https://www.example.com/
Careers: https://www.example.com/careers
"""


def main() -> int:
    print("1. Picks the real reset link out of a noisy email")
    url, n = pr.extract_reset_url(SUCCESSFACTORS, expect_domain="successfactors.eu")
    check(
        "SuccessFactors reset link",
        url is not None and "resetPassword" in url and "token=" in url,
        f"{str(url)[:56]} ({n} urls)",
    )
    check("SuccessFactors: not the privacy link", url is not None and "privacy" not in url)
    check("SuccessFactors: not the logo", url is not None and ".png" not in url)

    url, n = pr.extract_reset_url(WORKDAY, expect_domain="myworkdayjobs.com")
    check(
        "Workday reset link",
        url is not None and "reset-password" in url,
        f"{str(url)[:56]} ({n} urls)",
    )
    check("Workday: not the help link", url is not None and "/help" not in url)
    check("Workday: not the twitter link", url is not None and "twitter" not in url)

    url, n = pr.extract_reset_url(NOISY_FIRST_LINK, expect_domain="rippling.com")
    check(
        "Rippling: skips logo AND 'view in browser' first links",
        url is not None and "set-password" in url,
        f"{str(url)[:56]}",
    )

    print("\n2. Refuses when there is no reset link")
    url, n = pr.extract_reset_url(NO_RESET_LINK, expect_domain="example.com")
    check("plain acknowledgement email yields nothing", url is None, f"{n} urls seen")
    url, _ = pr.extract_reset_url("", expect_domain="x.com")
    check("empty body yields nothing", url is None)

    print("\n3. Domain safety - never hand back another service's reset link")
    url, _ = pr.extract_reset_url(SUCCESSFACTORS, expect_domain="totally-different.com")
    check(
        "reset link for a DIFFERENT domain is refused",
        url is None,
        "(would be a security bug)",
    )
    check(
        "related subdomain is accepted",
        pr.domains_related("career5.successfactors.eu", "career5.successfactors.eu"),
    )
    check(
        "same registrable domain, different subdomain",
        pr.domains_related("rippling.com", "ats.rippling.com"),
    )
    check(
        "unrelated domains rejected",
        not pr.domains_related("hazelton.com", "evil.example.net"),
    )
    check(
        "lookalike domain rejected",
        not pr.domains_related("rippling.com", "notrippling-phish.com"),
    )

    print("\n4. Directive parsing")
    cases = [
        ("NEED_PASSWORD_RESET: hazelton.com", "hazelton.com"),
        ("NEED_PASSWORD_RESET: ats.rippling.com", "ats.rippling.com"),
        ("some text\nNEED_PASSWORD_RESET: x.io\nmore", "x.io"),
        ("NEED_PASSWORD_RESET:", None),
        ("NEED_OTP: something", None),
    ]
    for line, want in cases:
        got = pr.parse_request(line)
        check(f"parse {line.splitlines()[0][:34]!r}", got == want, str(got))

    print("\n5. The password is the one the user specified")
    check(
        "standard password is Example-Passw0rd!",
        pr.STANDARD_PASSWORD == "Example-Passw0rd!",
        pr.STANDARD_PASSWORD,
    )

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nFailures:")
        for label, ok, note in results:
            if not ok:
                print(f"  {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
