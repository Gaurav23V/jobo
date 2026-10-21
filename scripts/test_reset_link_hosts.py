"""Offline replay of reset mails that arrived but were never handed over (GHO-62).

The `account:exists_password` backlog read as "the reset mail never came".
In Gmail, most of those mails were there within a minute of the request; the
lookup skipped them:

Job 1425 (iCIMS, 2026-10-11). Two "Reset your password" mails from icims.com.
The only link is a click tracker, clicks.icims.eu/f/a/<token>~~/<id>~/<token>~~,
whose "~" stopped the opaque-token score, so it scored under the floor.

Job 693 (UltiPro, 2026-10-02). "Reset your password" from notifications.ukg.net,
link on signin-us.ukg-prod.auth0app.com. The agent asked for ultipro.com,
and nothing tied ultipro.com to UKG's Auth0 tenant.

Job 794 (SuccessFactors, 2026-09-24). "Password Reset" from the employer's own
domain, link on career44.sapsf.com. The agent asked for the employer's domain,
then for successfactors.com; neither matched sapsf.com.

Bain-style Avature jobs (1481, 1594, 1708). The reset link was found, but the
portal refused the standard password as "one of your previous 3 passwords":
an earlier run had already set it. The agent reset before trying to sign in.

Names, hosts and tokens are fictional; the shapes are the real mails'.
No network, no Gmail, no browser.

Run:  bash scripts/run_regression_tests.sh test_reset_link_hosts
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from module4 import agent_driver, password_reset as pr, prompts  # noqa: E402
from test_emailed_mail_lookup import FakeGmail, reset_for  # noqa: E402

# --- fixtures -------------------------------------------------------------------

ICIMS_TRACKER = (
    "https://clicks.icims.eu/f/a/RmFrZVRyYWNrZXJUb2tlbjAx~~/AAAfxhA~/"
    "RgRfakeT0kenP0VodHRwczovL2xvZ2luLmljaW1zLmV1L3UvcmVzZXQtcGFzc3dvcmQ~~"
)
ICIMS_RESET = {
    "from": "iCIMS <noreply@icims.com>",
    "subject": "Reset your password",
    "body": (
        "<p>We received a request to change your password.</p>"
        f"<a href=\"{ICIMS_TRACKER}\">Reset password</a>"
        "<p>If you did not request this, ignore this email.</p>"
    ),
}

UKG_LINK = (
    "https://signin-us.ukg-prod.auth0app.com/u/reset-verify?"
    "ticket=FakeTicket0123456789abcdefABCDEF&ui_locales=en"
)
UKG_RESET = {
    "from": "UKG <no-reply@notifications.ukg.net>",
    "subject": "Reset your password",
    "body": (
        "Dear Candidate, You asked to change your password for your account. "
        f"Do you still want to change it? Yes, change my password ( {UKG_LINK} ) . "
        "If this was not you, check your account to be sure you can still log in. Thanks!"
    ),
}

SAPSF_LINK = (
    "https://career44.sapsf.com/career?company=hazeltonlt&amp;site=VjItZmFrZQ&amp;"
    "login%5fns=pwd%5freset&amp;account=ZmFrZUFjY291bnRUb2tlbg&amp;"
    "verify=ZmFrZVZlcmlmeVRva2Vu&amp;jobPipeline=LinkedIn"
)
EMPLOYER_SF_RESET = {
    "from": "Hazelton Careers <careers@hazelton.com>",
    "subject": "Password Reset",
    "body": (
        "<p>Dear Alex,<br/>Please click on the URL below to change your password."
        f"<br/><a href='{SAPSF_LINK}'>Click Here</a><br/>Best regards, Hazelton "
        "Recruitment Team.</p>"
    ),
}


def inbox(mail: dict) -> FakeGmail:
    g = FakeGmail()
    g.add(mail, age_sec=60)
    return g


def host(domain: str, mail: dict):
    return pr._host_of(reset_for(inbox(mail), domain).url or "") or None


# --- cases --------------------------------------------------------------------------


def lookup_cases() -> list[tuple[str, object, object]]:
    evil_auth0 = dict(UKG_RESET, body=UKG_RESET["body"].replace(
        "signin-us.ukg-prod.auth0app.com", "signin.harborpoint-prod.auth0app.com"))
    evil_auth0_label = dict(UKG_RESET, body=UKG_RESET["body"].replace(
        "signin-us.ukg-prod.auth0app.com", "notukg-prod.auth0app.com"))
    non_ats_link = dict(EMPLOYER_SF_RESET, body=EMPLOYER_SF_RESET["body"].replace(
        "career44.sapsf.com", "login.example.net"))
    other_sender = dict(EMPLOYER_SF_RESET, **{"from": "Harbor Point <careers@harborpoint.com>"})
    return [
        # 1425: the click tracker is the reset action.
        ("icims_tracker_link_found", "clicks.icims.eu", host("icims.eu", ICIMS_RESET)),
        ("icims_tracker_found_for_icims_com", "clicks.icims.eu", host("icims.com", ICIMS_RESET)),
        # 693: UltiPro's reset lives on UKG's Auth0 tenant.
        ("ultipro_ukg_auth0_link_found", "signin-us.ukg-prod.auth0app.com",
         host("ultipro.com", UKG_RESET)),
        ("ultipro_signin_host_link_found", "signin-us.ukg-prod.auth0app.com",
         host("signin-us.ultipro.com", UKG_RESET)),
        ("other_auth0_tenant_refused", None, host("ultipro.com", evil_auth0)),
        ("auth0_lookalike_label_refused", None, host("ultipro.com", evil_auth0_label)),
        # 794: employer-sent SuccessFactors mail, link on sapsf.com.
        ("employer_sent_sapsf_link_found", "career44.sapsf.com",
         host("hazelton.com", EMPLOYER_SF_RESET)),
        ("successfactors_com_covers_sapsf", "career44.sapsf.com",
         host("successfactors.com", EMPLOYER_SF_RESET)),
        ("successfactors_eu_covers_sapsf", "career44.sapsf.com",
         host("career5.successfactors.eu", EMPLOYER_SF_RESET)),
        ("sapsf_link_from_another_sender_refused", None, host("hazelton.com", other_sender)),
        ("employer_mail_non_ats_link_refused", None, host("hazelton.com", non_ats_link)),
        ("sapsf_link_for_unrelated_ask_refused", None,
         host("totally-different.com", EMPLOYER_SF_RESET)),
    ]


def unit_cases() -> list[tuple[str, object, object]]:
    rel = pr.domains_related
    return [
        ("family_needs_sender_or_family_ask", False,
         rel("hazelton.com", "career44.sapsf.com")),
        ("sender_display_name_alone_ignored", False,
         rel("hazelton.com", "career44.sapsf.com", "hazelton.com <x@harborpoint.com>")),
        ("sender_subdomain_counts", True,
         rel("hazelton.com", "career44.sapsf.com", "HR <hr@mail.hazelton.com>")),
        ("bare_auth0app_not_ukg", False, rel("ultipro.com", "auth0app.com")),
        ("old_brand_token_rule_kept", True, rel("rippling.com", "ats.rippling.com")),
        ("old_lookalike_rule_kept", False, rel("rippling.com", "notrippling-phish.com")),
    ]


def account_flow_cases() -> list[tuple[str, object, object]]:
    sp = prompts.SYSTEM_PROMPT
    section = sp[sp.index("## Locked-out accounts"):sp.index("## Account passwords")]
    sign_in = section.find(f"Sign in with {prompts.CANDIDATE_EMAIL} / {prompts.ACCOUNT_PASSWORD} first")
    forgot = section.find("Click the portal's \"Forgot password\"")
    hit = pr.ResetLinkResult(url="https://careers.hazelton.com/reset?token=abcdefghijklmnop",
                             subject="Password activation", sender="careers.hazelton.com")
    with patch.object(agent_driver.time, "sleep"), \
            patch.object(pr, "find_reset_link", return_value=hit), \
            patch("module4.linkedin_session.is_protected_account_domain", return_value=False):
        reply = agent_driver._resolve_password_reset(
            "NEED_PASSWORD_RESET: careers.hazelton.com", job_id=1708)
    return [
        ("prompt_has_sign_in_first_step", True, sign_in >= 0),
        ("prompt_sign_in_before_forgot", True, 0 <= sign_in < forgot),
        ("prompt_reused_password_means_sign_in", True, "used before" in section),
        ("reset_reply_still_gives_link", True, reply.startswith("PASSWORD_RESET_LINK: ")),
        ("reset_reply_reused_password_hint", True,
         "used before" in reply and "normal sign-in page" in reply),
    ]


def main() -> int:
    failed = total = 0
    for title, rows in (
        ("reset mails the lookup skipped (jobs 1425, 693, 794)", lookup_cases()),
        ("domain relatedness units", unit_cases()),
        ("existing account: sign in before reset (Avature, 1708)", account_flow_cases()),
    ):
        print(f"\n  -- {title} --")
        for label, expected, got in rows:
            ok = got == expected
            total += 1
            failed += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {label:46s} expected={expected!r} got={got!r}")
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
