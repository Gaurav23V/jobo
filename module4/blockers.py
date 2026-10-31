"""Failures that another attempt cannot fix, so the job is not retried.

Why: across all apply runs to 2026-09-26, 35% of every apply token went to
2nd and 3rd attempts at jobs that never got submitted. Most of those repeat
attempts hit the same wall as the first one: a form that demands data the user has
not provided (date of birth, PAN), a sign-in that only works through Google or
an Apple ID, an existing account whose password reset never arrives, a
captcha. The retry either spent a few calls re-discovering the wall or ground
through the whole form again first.

Deliberately conservative. A false "permanent" verdict drops a job the user could
have applied to, which is worse than one wasted retry, so each rule needs a
specific wall named in the agent's DETAIL, not just a keyword. Budget or
time exhaustion, upload trouble and dropdown trouble are NOT permanent: those
are the failures a retry (with more budget, or the deterministic handoffs)
does fix.
"""

from __future__ import annotations

import re

from module4 import address_wall, agent_driver, captcha_walls, google_forms

# status values that are permanent on their own
_PERMANENT_STATUSES = {
    # Captcha solving is only available as a paid cloud service; the agent
    # cannot pass these, and a retry meets the same challenge.
    "captcha_failed",
    # Only when it names no gate host (Oracle Cloud, iCIMS, Paycom, Lever, Cutshort): those are parked by the
    # runner before this is asked (module4/captcha_walls.py).
    "captcha_wall",
}

# A retry whose PREVIOUS failure was budget/time exhaustion already gets the
# full escalated budget (module4/agent_driver.py AGENT_STEPS_ESCALATED) on the
# theory that it was 90%+ through a long form and just needed more room - that
# escalation is deliberate and has its own evidence (2026-09-02/03 runs) of
# working, so a first or half-budget miss must NOT be treated as permanent
# here. But when the ESCALATED attempt itself ALSO exhausts its budget, the
# form needs more than the one escalation this job already got; another
# attempt would get the identical budget (_is_budget_exhaustion keeps
# escalating on every subsequent budget-exhaustion status) and, per the one
# measured case so far (job 936, Litware: escalated to 100 steps,
# still exhausted it, never recovered across all 3 attempts), the identical
# result. The phrase below is code-generated (module4/agent_driver.py
# run_apply_agent), never LLM prose, so it only ever appears when the code
# itself cut the attempt off at exactly that budget.
# LinkedIn's Easy Apply modal always carries an invisible reCAPTCHA
# Enterprise frame, on submitted applications too. Agents that got stuck on a
# Next/Submit that did not advance found the frame and called it a captcha:
# 1053, 1282, 1328, 1468, 1612 (2026-09-26 .. 10-14) were all dropped without
# retry as captcha_failed while 14 of the 17 Easy Apply attempts after the
# 10-12 click fixes went through. 1328 was not even on the modal (its frame
# was Chrome's new-tab page); 1612's Submit failed in the same session in
# which 1600 and 1690 submitted. No challenge was ever SHOWN in any of them,
# so these are ordinary stuck attempts: a retry gets its turn. A visible
# challenge (image grid, checkbox, puzzle) on LinkedIn stays permanent.
_LINKEDIN_EASY_APPLY = re.compile(r"linkedin|easy apply", re.I)
_INVISIBLE_RECAPTCHA = re.compile(r"invisible (?:google )?re-?captcha|re-?captcha enterprise", re.I)
_VISIBLE_CHALLENGE = re.compile(
    r"image|puzzle|checkbox|select all|click (?:all|the)|drag|slider|audio|"
    r"i'?m not a robot|visual challenge|challenge (?:appeared|was shown|popped)",
    re.I,
)


def _is_easy_apply_invisible_recaptcha(detail: str) -> bool:
    return bool(
        _LINKEDIN_EASY_APPLY.search(detail)
        and _INVISIBLE_RECAPTCHA.search(detail)
        and not _VISIBLE_CHALLENGE.search(detail)
    )


_ESCALATED_BUDGET_STILL_EXHAUSTED = re.compile(
    rf"ran out of step budget \({agent_driver.AGENT_STEPS_ESCALATED} model steps\)"
)

# A photo of the candidate. G skips forms that require one (GHO-60), so these
# are a policy skip, not a stuck job. The agent words it many ways: "photo
# image upload" (Workable 3096), "Candidate Photo" upload (Deloitte 1590),
# "passport-style photo", "profile picture upload", "upload a headshot".
_PHOTO = (
    r"(?:photo(?:graph)?|picture|image)[\"'”’]?(?: image| file)? upload|"
    r"upload (?:of )?(?:a |an |your )?(?:photo(?:graph)?|picture|headshot)|"
    r"passport[- ](?:style|size|sized) (?:photo(?:graph)?|picture)|candidate photo(?:graph)?|headshot"
)
_PERSONAL_DATA = re.compile(
    r"\b(?:date of birth|d\.?o\.?b\.?|pan(?: card| number)?|aadhaa?r|national (?:id|identifier)|"
    rf"social security|ssn|passport number|{_PHOTO})\b",
    re.I,
)
_DEMANDS = re.compile(r"requir|mandat|compulsory|cannot (?:be )?(?:fabricat|invent)|not (?:in|present in|available in) (?:the )?candidate", re.I)
_PHOTO_RE = re.compile(rf"\b(?:{_PHOTO})\b", re.I)
PHOTO_SKIP_STATUS = "skipped: G decision (GHO-60): photo upload required"


def photo_required(detail: str | None) -> bool:
    """The form demands a photo of the candidate (skipped by G's GHO-60 rule)."""
    d = detail or ""
    return bool(_PHOTO_RE.search(d) and _DEMANDS.search(d))


def address_park(detail: str | None) -> bool:
    """The form stops on a street address / postal code the candidate facts
    lack, and on nothing a G answer about the address would not clear: a
    photo, date of birth or ID number it also demands keeps its own verdict.
    Parked, not dropped (module4/address_wall.py)."""
    d = detail or ""
    return address_wall.is_address_wall(d) and not (_PERSONAL_DATA.search(d) and _DEMANDS.search(d))

_EXTERNAL_IDENTITY_ONLY = re.compile(
    r"only (?:offers|supports|allows|accepts|has) (?:an? )?(?:oauth|google|sso|linkedin|microsoft|apple)|"
    # The button's own wording, often quoted: 'only offers "Continue with
    # Google" sign-in' (job 2046, 2026-10-21).
    r"only (?:offers|supports|allows|accepts|has) (?:an? )?[\"'“‘]?"
    r"(?:continue|sign[- ]?in|log[- ]?in|sign[- ]?up) with (?:google|linkedin|microsoft|apple)|"
    r"oauth[- ]only|sso[- ]only|apple id|trusted[- ]device|sms[- ]2fa|sms[- ]based 2fa|"
    r"requires (?:a )?google (?:account )?sign[- ]in",
    re.I,
)
# A Google FORM behind Google sign-in is not a dead end: one Google sign-in in
# the apply browser opens it. module4/google_forms.py parks those jobs (no
# attempt charged) instead of dropping them. Until 2026-10-03 this file
# matched "google form that requires ..." and dropped 5 jobs for good.
_ORG_ONLY_FORM = re.compile(
    r"only be viewed by users in|users in the owner'?s organi[sz]ation|"
    r"google form.{0,80}?(?:restricted|limited) to (?:users|members|accounts) (?:in|of) ",
    re.I | re.S,
)

_ACCOUNT_EXISTS = re.compile(
    r"account (?:for \S+ )?(?:already )?exists|existing account|pre-existing account|already registered|"
    r"account \(\S+\) exists|already has an account",
    re.I,
)

# An ATS account wall the agent cannot get through, on its own: such
# jobs are skipped, not retried. Seen in the
# 2026-10-11 run: Workday tenants whose Create Account / Sign In / Forgot
# password buttons do nothing (Willowby 1464, Adatum 1513; Gmail shows no
# mail from either tenant ever, so no account exists to recover), and Avature
# refusing our standard password because of its password-history rule (Wingtip
# 1594: "must be different from previous 3 passwords"). Each phrase names the
# wall itself; a generic "stuck on sign in" is NOT enough, because a form that
# merely needs one more click must still get its retry.
_ACCOUNT_WALL = re.compile(
    r"no account could be (?:created|recovered)|"
    r"account creation (?:silently )?fail|"
    r"(?:cannot|could not|can't) (?:progress|get|proceed) past (?:the )?"
    r"(?:account creation|create account|account)|"
    r"create account.{0,200}?(?:no (?:response|visible state change|navigation|page-state change)|"
    r"unresponsive|nothing happen|did nothing|silently)|"
    r"(?:previous|last) \d+ passwords|previously[- ]used password|password was last changed",
    re.I | re.S,
)
_RECOVERY_FAILED = re.compile(
    r"(?:password reset|reset (?:email|link|request)|recover|verification (?:email|link)|verify)"
    r".{0,160}?(?:fail|never|not (?:received|arriv|deliver|sent)|no (?:email|code|link|reset)|"
    r"produced no|did not|didn't|unresponsive|silently|no page-state change)",
    re.I | re.S,
)

_UNDELIVERABLE_CODE = re.compile(
    r"(?:otp|verification code|email code).{0,120}?(?:undeliverab|non-functional|never (?:arrives|delivered))",
    re.I | re.S,
)

# "mandatory X ... not present in candidate facts": the form wants data the user has
# not supplied (10th/12th percentages, current company). Adding the fact to
# module4/prompts.py CANDIDATE_FACTS and re-queueing with
# scripts/reset_attempts.py is the fix, not another attempt.
_MISSING_FACT = re.compile(
    r"(?:requir|mandat|compulsory).{0,200}?not (?:present |available )?in (?:the )?candidate facts",
    re.I | re.S,
)

# A required question none of whose choices is true for the candidate (a
# location dropdown offering only US/Canada, job 2173): the agent must not pick
# a false answer, and the next attempt meets the same list. The agent's own
# words name it; a dropdown that merely would not keep a value is not this.
_NO_TRUE_OPTION = re.compile(
    r"requir.{0,200}?(?:no (?:option|answer|choice)s? (?:is|are|was|were) true|"
    r"no true (?:option|answer|choice|match))",
    re.I | re.S,
)

# A ZipRecruiter apply link that answers with a server error on every load in
# the attempt is a listing ZipRecruiter no longer serves (job 1868: "Internal
# Server Error" in the 2026-10-18 and 10-21 runs, the posting gone). Treated as
# closed, not as a failure, so it neither burns retries nor counts as stuck.
# One failed load is not enough: the agent must say it persisted.
_ZIPRECRUITER = re.compile(r"ziprecruiter", re.I)
_SERVER_ERROR = re.compile(r"internal server error|server error|\b5\d\d\b|something went wrong", re.I)
_PERSISTED = re.compile(
    r"consistently|repeated(?:ly)?|persistent(?:ly)?|every (?:attempt|time|load|try|reload)|"
    r"(?:on|after) (?:both|each|all|two|three|\d+) (?:\w+ )?(?:attempts|tries|loads|reloads)|"
    r"(?:two|three|\d+) (?:attempts|tries|loads|reloads|times)|"
    # A later attempt seeing it again: "... again on this retry" (10-22).
    r"again (?:on|in|after) (?:this|the|a|another) (?:retry|reload|attempt|run)",
    re.I,
)

# A job page that offers no way at all to apply: no form, no apply button and
# no email address (sedbox 2886, 2026-10-30: a Google Sites page holding only
# the description). Closed, like a posting that says so itself (1940). The
# agent must say it looked for an email too, and a page that did not render
# (iframe, captcha, sign-in, a load failure) is never this.
_NO_WAY_TO_APPLY = re.compile(
    r"no (?:apply|application) (?:form|button|link|control)s?\b[^.;]{0,60}?\b(?:or|nor)\b "
    r"(?:an? |any )?(?:apply |application |contact )?e-?mail\b",
    re.I,
)
_PAGE_NOT_SEEN = re.compile(
    r"iframe|embed|captcha|sign[- ]?in|log[- ]?in|did not (?:load|render)|failed to load|"
    r"not load|blank|spinner|timed? ?out|blocked|error",
    re.I,
)

# An apply link that only ever opens the employer's whole job board, where
# nothing marks which posting is this job (job 3481, 2026-10-31: a JobDiva ?a=
# link landed on a 465-job search board holding several postings with the
# same title). Closed: no attempt can reach a posting it cannot name. Both
# halves must be there; a board where the agent can still find the job is not
# this.
_GENERIC_BOARD = re.compile(
    r"(?:never|not|only|just)\b[^.;]{0,120}?\b(?:generic|general|company-wide|whole|full|all[- ]jobs?)\b"
    r"[^.;]{0,40}?\b(?:job |jobs |search |career |careers )?(?:board|search|listings?|job list|careers? (?:page|site))\b",
    re.I,
)
_POSTING_UNIDENTIFIABLE = re.compile(
    r"no way to (?:identify|tell|know|determine|pick) which|"
    r"(?:could not|cannot|can'?t|unable to) (?:identify|tell|determine) which",
    re.I,
)

# The employer's own eligibility screen turns the candidate away: work
# authorization / sponsorship pre-screen questions, answered truthfully, end
# the application at once (job 3480, 2026-10-31: auto-rejected before the
# main form with only "Thank you for your interest."). A knockout, not a
# broken form: every attempt gives the same true answers. Trouble setting the
# answer (a sponsorship dropdown that will not keep its value) is not this.
_ELIGIBILITY_SCREEN = re.compile(
    r"work authori[sz]ation|sponsorship|right to work|legally (?:authori[sz]ed|eligible)",
    re.I,
)
_KNOCKOUT = re.compile(
    r"auto-?reject|knock(?:ed)?[- ]?out|disqualif|screened (?:me |the candidate )?out|"
    r"reject(?:s|ed)? (?:the candidate |the application |it )?(?:at once|immediately|instantly)",
    re.I,
)

# The employer's own server cannot be reached (job 2451: the employer's job page
# printed "Failed to connect to <backend> port 443" on 25 Oct and again on
# 30 Oct; still so the same night). One run's failure is a temporary outage
# and gets its retry; the same host unreachable again on the next run is a
# dead site, closed like a dead listing. Our own browser or network failing
# (CDP on 127.0.0.1, LinkedIn itself unreachable) is never this.
_UNREACHABLE = re.compile(
    r"failed to connect to|could not connect to (?:the )?server|"
    r"err_(?:connection_(?:refused|timed_out|reset|closed)|name_not_resolved|address_unreachable)|"
    r"dns_probe_\w+|site can'?t be reached|connection (?:refused|timed out)",
    re.I,
)
_OUR_SIDE = re.compile(r"127\.0\.0\.1|localhost|\bcdp\b|browser session|linkedin\.com", re.I)
_HOST_NAME = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)


def _unreachable_hosts(detail: str) -> set[str]:
    """Hosts named in a report of an employer site that cannot be reached."""
    if not _UNREACHABLE.search(detail) or _OUR_SIDE.search(detail):
        return set()
    return {h.lower().removeprefix("www.") for h in _HOST_NAME.findall(detail)}


_NO_WEB_FORM = re.compile(r"mailto:|no web (?:application )?form|apply by email only", re.I)
_BOT_WALL = re.compile(r"cloudflare|bot[- ]protection|bot[- ]fingerprint", re.I)


def permanent_reason(status: str | None, detail: str | None) -> str | None:
    """A short reason when the failure cannot be fixed by retrying, else None."""
    st = (status or "").strip().lower()
    d = detail or ""
    # A signed-out LinkedIn session is the BROWSER's state, never the job's:
    # once the user signs in again every one of these jobs can go through. Checked
    # first because the agent's wording ("Check your LinkedIn app ... SMS
    # verification", "Google OAuth or email/password") can otherwise trip the
    # external-identity rule below (jobs 1007, 1140, 2026-10-03).
    from module4 import linkedin_session

    if linkedin_session.is_signed_out_outcome(st, d):
        return None
    if st == "captcha_failed" and _is_easy_apply_invisible_recaptcha(d):
        return None
    if st == "captcha_wall" and captcha_walls.gate_host_in_text(d):
        return None  # parked, not dropped: see module4/captcha_walls.py
    if st in _PERMANENT_STATUSES:
        return f"{st}: retries meet the same challenge"
    if _ESCALATED_BUDGET_STILL_EXHAUSTED.search(d):
        return "form still exceeds the escalated step budget: a further retry gets the identical budget and the same result"
    if address_park(d):
        return None  # parked, not dropped: see module4/address_wall.py
    if _PERSONAL_DATA.search(d) and _DEMANDS.search(d):
        return "form requires personal data that is not in the candidate facts"
    if _MISSING_FACT.search(d):
        return "form requires data that is not in the candidate facts"
    if _NO_TRUE_OPTION.search(d):
        return "a required question has no option that is true for the candidate"
    if _ORG_ONLY_FORM.search(d):
        # Seen only once signed in: the form is limited to the owner's
        # Workspace domain, so no account of ours can open it.
        return "Google Form limited to the owner's organisation"
    if google_forms.is_parked(f"{st}:{d}") or google_forms.is_signin_wall(d):
        return None  # parked, not dropped: see module4/google_forms.py
    if _EXTERNAL_IDENTITY_ONLY.search(d):
        return "sign-in only via an external identity (Google/SSO/Apple ID/2FA device)"
    if _ACCOUNT_EXISTS.search(d) and _RECOVERY_FAILED.search(d):
        return "existing ATS account that could not be recovered"
    if _ACCOUNT_WALL.search(d):
        return "ATS account wall: account cannot be created or signed in to"
    if _UNDELIVERABLE_CODE.search(d):
        return "verification code is never delivered"
    if _NO_WEB_FORM.search(d) and not _email_apply_on():
        return "no web application form"
    if _BOT_WALL.search(d):
        return "site blocks automated browsers"
    return None


def dead_listing_reason(status: str | None, detail: str | None, url: str | None = None,
                        *, previous: str | None = None) -> str | None:
    """A short reason when a stuck attempt really met a listing that is gone,
    or one that is shut to this candidate (an eligibility knockout).

    previous: the job's stored module4_last_status from its last run, when
    the attempt being judged is a new one.
    """
    if (status or "").strip().lower() != "stuck":
        return None
    d = detail or ""
    if _NO_WAY_TO_APPLY.search(d) and not _PAGE_NOT_SEEN.search(d):
        return "job page has no apply form, button or email address"
    if _GENERIC_BOARD.search(d) and _POSTING_UNIDENTIFIABLE.search(d):
        return "apply link opens only a generic job board, with no way to identify the posting"
    if _ELIGIBILITY_SCREEN.search(d) and _KNOCKOUT.search(d):
        return "employer's eligibility screen rejects the candidate (work authorization / sponsorship)"
    prev_status, _, prev_detail = (previous or "").partition(":")
    if prev_status.strip().lower() == "stuck" and (
        _unreachable_hosts(d) & _unreachable_hosts(prev_detail)
    ):
        return "employer site unreachable on two runs in a row"
    if not (_ZIPRECRUITER.search(d) or _ZIPRECRUITER.search(url or "")):
        return None
    if _SERVER_ERROR.search(d) and _PERSISTED.search(d):
        return "ZipRecruiter apply link answers a server error on every load"
    return None


def _email_apply_on() -> bool:
    # A mailto-only posting is applied to by email now (module4/email_apply.py);
    # the retry lets the agent report it as `email_apply`.
    from module4 import email_apply

    return email_apply.mode() != "off"


def permanent_reason_from_last_status(last_status: str | None) -> str | None:
    """Same check on a stored module4_last_status ("<status>:<detail>")."""
    s = last_status or ""
    status, _, detail = s.partition(":")
    return permanent_reason(status, detail)
