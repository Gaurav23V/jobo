"""Retrieve one-time verification codes from the user's Gmail for the apply agent.

Why this exists
---------------
On the 2026-09-01 apply run, 7 of 19 failures were ATS portals that gate the
application form behind an emailed 6-digit code (Northwind MyCareer / Azure
B2C, Adatum Workday, Shine.com, SuccessFactors, TurboHire). The browser
agent could fill every field but had no way to read the inbox, so each of
those jobs died at the verification step.

Module 1 already holds Gmail OAuth credentials with `gmail.modify` scope for
the collector, so the capability was sitting right there unused. This module
exposes a deliberately NARROW read path over those same credentials:

  * only messages newer than a caller-specified window (default 10 min), so
    an agent can never pull an unrelated historical code
  * only the numeric/alphanumeric code is returned, never full message bodies,
    so an application agent can't be steered into exfiltrating inbox content
  * read-only in effect: nothing is deleted, nothing is marked, no drafts

Design notes
------------
`extract_code` is intentionally strict about what counts as a code and about
rejecting near-miss numbers that live in verification emails (years, phone
numbers, money amounts, street numbers). It is unit-testable in isolation
without any network access - see scripts/test_otp_extraction.py.

The `wait_for_code` polling loop exists because the agent typically triggers
"send me a code" and then needs to block for a few seconds; a single lookup
would usually race the email's arrival.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = REPO_ROOT / "credentials" / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Words that reliably appear near a genuine verification code. Used both to
# rank candidate messages and to locate the code inside a body.
CODE_CONTEXT_WORDS = (
    "verification code",
    "verify your email",
    "verification",
    "one-time",
    "one time",
    "onetime",
    "otp",
    "passcode",
    "pass code",
    "security code",
    "access code",
    "confirmation code",
    "confirm your email",
    "authentication code",
    "login code",
    "sign-in code",
    "sign in code",
    "your code",
    "code is",
    "temporary code",
    "activation code",
    "pin",
)

# Patterns that look like codes but are not. Checked against the immediate
# textual neighbourhood of a candidate match.
_NEGATIVE_CONTEXT = (
    "phone",
    "mobile",
    "tel:",
    "zip",
    "postal",
    "invoice",
    "order #",
    "order no",
    "reference number",
    "employee id",
    "req id",
    "requisition",
)

# A code is 4-8 chars: digits, or MIXED-CASE alphanumeric. Bounded by non-alnum
# so we don't grab a slice out of a longer identifier.
#
# The alpha branch used to be [A-Z0-9]{6,8} (uppercase only), which silently
# discarded every Greenhouse security code - they are mixed case, e.g.
# "kRNWpqzs", "R3hTKGwN", "ppqZvM4n". Observed 2026-09-05: 5/5 codes missed,
# 4 otherwise-complete applications reported "no email arrived" while the mail
# sat in the inbox. Widening to [A-Za-z0-9] alone is not safe either - it
# matches ordinary words like "Alexander" or "Alderly" - so every alpha match is
# additionally screened by _looks_like_word().
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([0-9]{4,8}|[A-Za-z0-9]{6,8})(?![A-Za-z0-9])")

# Obvious non-codes even when they match the shape above.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# A plain English word or a product/company name: all lowercase, a single
# leading capital, or CamelCase ("CodeHarbor", "Tamarack"). Real codes are not
# shaped like this - they either carry a digit or scatter capitals mid-run in
# a way names do not ("kRNWpqzs" capitalises position 2 and 4-5).
_WORD_RE = re.compile(r"^(?:[A-Z][a-z]+)+$|^[a-z]+$|^[A-Z]+$")


def _looks_like_word(token: str) -> bool:
    """True if `token` is prose or a brand name rather than a code.

    Only consulted for tokens containing letters; pure digit runs are handled
    by the year/negative-context checks. A token qualifies as a code when it
    mixes letters with digits, or scatters capitals in a way names do not.

    "CodeHarbor" in the subject line ("Security code for your application to
    CodeHarbor") was returned as the code before the CamelCase branch existed.
    """
    if token.isdigit():
        return False
    if any(c.isdigit() for c in token):
        # Letters AND digits together is decisive - no name does that.
        return False
    return bool(_WORD_RE.match(token))

# Codes are frequently rendered as separated digit groups: SuccessFactors
# styles them as "44 12 09", and any HTML mail that puts each digit in its own
# <td> becomes "3 1 4 1 5 9" once tags are stripped. Match a run of 2+ short
# digit groups whose digits total 4-8, then join them.
_SPACED_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,3}(?:[ \t\u00a0]+\d{1,3}){1,7})(?![A-Za-z0-9])")

# Some portals skip codes entirely and mail a one-click sign-in URL. Ranked
# highest-first: a token-bearing auth path is the real link, while the same
# mail also carries logo, unsubscribe and help-centre URLs.
_MAGIC_LINK_PATTERNS: tuple[str, ...] = (
    r"https?://[^\s\"'<>]*(?:magic-link|magiclink)[^\s\"'<>]*",
    r"https?://[^\s\"'<>]*/(?:auth|login|signin|sign-in)/[^\s\"'<>]*token=[^\s\"'<>]+",
    r"https?://[^\s\"'<>]*(?:verify|confirm)[^\s\"'<>]*token=[^\s\"'<>]+",
    r"https?://[^\s\"'<>]*token=[A-Za-z0-9._-]{16,}[^\s\"'<>]*",
    # Account activation with the token as a path segment. Workday's
    # "Verify your candidate account" mail carries only
    # https://halv.wd3.myworkdayjobs.com/HALV_Careers/activate/<70 chars>/?redirect=...
    # (job 1523, 2026-10-14: found on every poll, skipped as "no code").
    r"https?://[^\s\"'<>]*/(?:activate|activation|verify-email|confirm-email)/[A-Za-z0-9._-]{16,}[^\s\"'<>]*",
)

# Wording that means "this mail carries a sign-in link, not a code".
_MAGIC_LINK_CONTEXT = (
    "magic link",
    "authentication link",
    "sign in to",
    "sign-in link",
    "login link",
    "click the button below",
    "securely sign in",
    "confirm your email",
    "verify your email",
    "activate your account",
    "verify your candidate account",
)


def extract_magic_link(text: str, subject: str = "") -> str:
    """Return a one-click sign-in URL from `text`, or "".

    Requires BOTH link-shaped evidence and sign-in wording, so an ordinary
    mail containing some URL with a token query param is not mistaken for an
    auth link.
    """
    if not text:
        return ""
    plain = _strip_html(text)
    hay = f"{subject}\n{plain}".lower()
    if not any(w in hay for w in _MAGIC_LINK_CONTEXT):
        return ""
    for pattern in _MAGIC_LINK_PATTERNS:
        m = re.search(pattern, plain, re.IGNORECASE)
        if m:
            return m.group(0).rstrip(".,);]>\"'")
    return ""


def _join_spaced(fragment: str) -> str:
    return re.sub(r"[ \t\u00a0]+", "", fragment)


def _spaced_candidates(window: str) -> list[str]:
    """Digit-group runs in `window` collapsed into plausible codes."""
    out = []
    for m in _SPACED_CODE_RE.finditer(window):
        joined = _join_spaced(m.group(1))
        if 4 <= len(joined) <= 8 and not _YEAR_RE.match(joined):
            out.append(joined)
    return out


@dataclass
class OtpResult:
    code: Optional[str]
    sender: str = ""
    subject: str = ""
    received_at: str = ""
    searched_query: str = ""
    candidates_scanned: int = 0
    error: str = ""
    # Some portals send a one-click sign-in URL INSTEAD of a code (Goodfit,
    # Notion-style "magic links"). There is no code to type, so a code-only
    # search reports "no email arrived" while a perfectly usable link sits in
    # the inbox (observed 2026-09-05, job651).
    magic_link: str = ""
    # A mail arrived that EXPLAINS why there is no code (e.g. "you already
    # have an account"). Retrying cannot help; the agent needs to change
    # tactic, so this is surfaced separately from a plain "nothing found".
    blocker: str = ""
    # Gmail message id of the mail used, so a parallel run can record which
    # job consumed it (module4/apply_queue.py, otp_consumed).
    message_id: str = ""

    @property
    def found(self) -> bool:
        return bool(self.code)

    @property
    def actionable(self) -> bool:
        """A code OR a sign-in link - either lets the agent make progress."""
        return bool(self.code or self.magic_link)


# Messages that arrive INSTEAD of a code and mean "stop asking". Without
# these, an agent burns every OTP round and its whole turn budget waiting for
# a code that will never be sent. Observed 2026-09-02: 5 Northwind jobs failed
# this way after an earlier job had already registered the email address, and
# every subsequent signup attempt got "there is already an account connected
# to it" with no code in the body.
NO_CODE_EXPLANATIONS: tuple[tuple[str, str], ...] = (
    (
        r"already an account connected|already have an account|account already exists"
        r"|already registered",
        "an account already exists for this email address, so no signup code will be "
        "sent. Do NOT keep requesting a code. Use the Sign In page instead of Sign Up; "
        "if you cannot sign in because the password is unknown, report status=stuck "
        "with the detail 'existing ATS account, password unknown'.",
    ),
    (
        r"too many (attempts|requests)|rate.?limit|try again later",
        "the portal is rate-limiting verification emails. Do not keep requesting "
        "codes; report status=stuck noting the rate limit.",
    ),
    (
        r"account (has been )?(locked|suspended|disabled)",
        "the account is locked or suspended, so no code will arrive. Report "
        "status=stuck with that reason.",
    ),
)


def _load_credentials():
    """Load the collector's existing Gmail credentials, refreshing if needed.

    Deliberately does NOT fall back to an interactive OAuth flow: this runs
    inside an unattended apply run, where opening a browser consent page would
    hang the pipeline. A dead token is an error the caller must surface.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"No Gmail token at {TOKEN_FILE}. Run `uv run python auth_manual.py` first."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist the refreshed access token so sibling processes benefit.
        try:
            TOKEN_FILE.write_text(creds.to_json())
        except OSError as e:  # pragma: no cover - best effort
            logger.debug("Could not persist refreshed token: %s", e)
    if not creds or not creds.valid:
        raise RuntimeError(
            "Gmail credentials are not valid (token likely expired or revoked). "
            "Re-auth with `uv run python auth_manual.py`."
        )
    return creds


def _gmail_service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)


def _decode_b64url(data: str) -> str:
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _walk_body(payload: dict, keep_html: bool = False) -> str:
    """Flatten a Gmail payload into searchable text (plain preferred).

    keep_html: return an HTML-only body untouched, so a caller that needs
    link targets (password_reset) can read the hrefs before stripping tags.
    """
    plain, html = [], []

    def visit(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if "data" in body:
            try:
                decoded = _decode_b64url(body["data"])
            except Exception:  # pragma: no cover - malformed part
                decoded = ""
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for sub in part.get("parts", []) or []:
            visit(sub)

    visit(payload)
    if plain:
        return "\n".join(plain)
    if html and keep_html:
        return "\n".join(html)
    if html:
        # Strip tags cheaply; we only need numbers and nearby words. Insert
        # spaces so `<td>1</td><td>2</td>` doesn't fuse into "12".
        text = re.sub(r"<[^>]+>", " ", "\n".join(html))
        text = text.replace("&nbsp;", " ").replace("&zwnj;", "")
        return re.sub(r"[ \t]+", " ", text)
    return ""


def _looks_like_year(token: str) -> bool:
    return bool(_YEAR_RE.match(token))


# Context words must stand alone. A plain substring test let "otp" match
# LinkedIn's `otpToken=` URL parameter and "pin" match "shopping".
_CONTEXT_RES = tuple(
    (word, re.compile(rf"(?<![a-z0-9]){re.escape(word)}s?(?![a-z0-9])"))
    for word in CODE_CONTEXT_WORDS
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# In strict mode a token in the subject line only counts when the subject is
# about a code ("6-digit Code for your application ... - 639254"). Without
# this, any 6-8 char mixed token in an unrelated subject would qualify.
_SUBJECT_CODE_WORD_RE = re.compile(
    r"(?<![a-z0-9])(code|otp|pin|passcode|password|verification|verify)(?![a-z0-9])"
)


def _strip_html(text: str) -> str:
    """Tags -> spaces, so per-digit <td> cells become separated digit groups.

    _walk_body already does this for messages it decodes, but extract_code is
    also the public/testable entry point and may be handed raw HTML directly;
    normalizing here means both paths behave identically.
    """
    if "<" not in text:
        return text
    out = re.sub(r"<[^>]+>", " ", text)
    out = out.replace("&nbsp;", " ").replace("&zwnj;", "")
    return re.sub(r"[ \t]+", " ", out)


def extract_code(text: str, subject: str = "", require_context: bool = False) -> Optional[str]:
    """Pull the most probable verification code out of message text.

    Strategy, in priority order:
      1. A code appearing immediately after a context phrase ("your code is
         123456") - strongest signal.
      2. A code in the subject line (many ATS mails put it there).
      3. A standalone code on its own line in the body (common in templated
         mails where the code sits in a big styled block).
      4. Any remaining shape-match, preferring 6 digits (the overwhelmingly
         common length) and rejecting years / negative contexts.

    require_context=True disables steps 3 and 4, so a code is only returned
    when it sits next to explicit verification wording. Used when searching
    WITHOUT a sender filter, where a loose shape-match could otherwise pick a
    number out of an unrelated email.
    """
    if not text and not subject:
        return None

    text = _strip_html(text)
    # URLs are removed before ANY matching. Tracking links are full of
    # code-shaped tokens next to code-ish words: every LinkedIn notification
    # carries "...-null-qz7k2m~xp4mtrlc~w7...&eid=qz7k2m-...&otpToken=...",
    # so "otp" + "qz7k2m" looked like a labelled code. On 2026-09-29 that
    # token was typed into Humongo's verification page twice (job 1007). A real
    # code is shown to the reader, never only inside a link.
    text = _URL_RE.sub(" ", text)
    haystack = f"{subject}\n{text}"
    lowered = haystack.lower()

    # 1. Context-phrase adjacency.
    context_hits: list[str] = []
    for word, word_re in _CONTEXT_RES:
        for wm in word_re.finditer(lowered):
            idx = wm.start()
            # Look in a window after the phrase (codes follow the label), and
            # a small window before it ("123456 is your verification code").
            after = haystack[idx + len(word) : idx + len(word) + 60]
            before = haystack[max(0, idx - 40) : idx]
            for window in (after, before):
                for m in _CODE_RE.finditer(window):
                    token = m.group(1)
                    if _looks_like_year(token) or _looks_like_word(token):
                        continue
                    neighbourhood = window[max(0, m.start() - 30) : m.end() + 30].lower()
                    if any(neg in neighbourhood for neg in _NEGATIVE_CONTEXT):
                        continue
                    context_hits.append(token)
                # Same window, but for codes rendered as separated digit
                # groups ("44 12 09", or one <td> per digit).
                for joined in _spaced_candidates(window):
                    if not any(neg in window.lower() for neg in _NEGATIVE_CONTEXT):
                        context_hits.append(joined)

    subject_hits: list[str] = []
    # 2. Subject line. Strict mode only trusts a subject that talks about a
    # code ("Your OTP ...", "6-digit Code ... - 639254").
    if subject and (not require_context or _SUBJECT_CODE_WORD_RE.search(subject.lower())):
        for m in _CODE_RE.finditer(subject):
            token = m.group(1)
            if not _looks_like_year(token) and not _looks_like_word(token):
                subject_hits.append(token)

    # A letters-only hit is the weakest kind: a brand name in the subject
    # ("Security code for your application to Eastlake", "... to OpenAI")
    # passes _looks_like_word() whenever it is not plain CamelCase, and it
    # sits right after the word "code". On 2026-10-04 "Eastlake" was returned
    # and typed into Greenhouse's 8-character field (job 1266) while the real
    # code was alone on its own line in the body. So a hit with a digit wins
    # first; then a letters-only hit that is also alone on a line (Greenhouse
    # puts "kRNWpqzs" there); only then the old first-match order.
    own_lines = {line.strip() for line in text.splitlines()}
    for token in context_hits + subject_hits:
        if not token.isalpha():
            return token
    for token in context_hits + subject_hits:
        if token in own_lines:
            return token
    if require_context or not any(
        _CODE_RE.fullmatch(line)
        and not _looks_like_year(line)
        and not _looks_like_word(line)
        for line in own_lines
    ):
        if context_hits or subject_hits:
            return (context_hits + subject_hits)[0]

    if require_context:
        # No explicit verification wording found, and we are not allowed to
        # guess from shape alone (unfiltered-sender search).
        return None

    # 3. Standalone on its own line.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _CODE_RE.fullmatch(stripped)
        if m and not _looks_like_year(m.group(1)) and not _looks_like_word(m.group(1)):
            return m.group(1)

    # 4. Fallback: prefer 6-digit, then any digit run.
    # URLs are excluded: query strings and UUID path segments are full of
    # digit runs that look exactly like codes ("...d103-7754-9d5c...").
    text_nolinks = re.sub(r"https?://\S+", " ", text)
    candidates = []
    for m in _CODE_RE.finditer(text_nolinks):
        token = m.group(1)
        if _looks_like_year(token) or _looks_like_word(token):
            continue
        # Screen the words immediately BEFORE the token. Without this the
        # Greenhouse footer address "18 West 18th Street ... New York, NY
        # 10011" yielded 10011 as the verification code (observed
        # 2026-09-05). Only the preceding context is checked: a negative word
        # AFTER a code does not disqualify it, or "Your security code is
        # 337914.\nRequisition 2024..." would lose a perfectly good code.
        neighbourhood = text_nolinks[max(0, m.start() - 40) : m.start()].lower()
        if any(neg in neighbourhood for neg in _NEGATIVE_CONTEXT):
            continue
        candidates.append(token)
    if not candidates:
        return None
    for length in (6, 5, 4, 7, 8):
        for c in candidates:
            if len(c) == length and c.isdigit():
                return c
    return candidates[0]


# --- Whose mail may supply a code --------------------------------------------
# On 2026-09-29 the agent asked `NEED_OTP: paradox.ai | Humongo verification
# code`. The made-up subject matched nothing, the unfiltered fallback then
# took "qz7k2m" from a LinkedIn "your application was sent to Brightwave
# Staffing" mail, and that string was typed into Humongo's verification page
# twice (job 1007). The same fallback had already handed LinkedIn tokens to
# Fourthco/Workday (job 683, 2026-09-18) and Greenhouse (job 798, 2026-09-29).
# A wrong code is worse than no code, so the fallback now also checks WHO sent
# the mail, not only what it says:
#   * job boards and their notification senders never supply a code unless
#     the agent asked for that board's own code (e.g. a LinkedIn sign-in PIN);
#   * when the agent named a site, the mail must show that site in its From
#     header (address or display name) or carry the site's domain in its body.

# Mail from these domains is job-board traffic: application receipts, alerts,
# digests. Their bodies are full of code-shaped tracking tokens.
JOB_BOARD_DOMAINS: tuple[str, ...] = (
    "linkedin.com",
    "naukri.com",
    "indeed.com",
    "indeedemail.com",
    "glassdoor.com",
    "glassdoor.co.in",
    "monster.com",
    "foundit.in",
    "instahyre.com",
    "internshala.com",
    "wellfound.com",
    "angel.co",
    "cutshort.io",
    "hirist.com",
    "hirist.tech",
    "iimjobs.com",
    "shine.com",
    "timesjobs.com",
    "apna.co",
    "ziprecruiter.com",
    "dice.com",
    "simplyhired.com",
    "unstop.com",
)
# Local parts only job boards use, whatever the domain.
_JOB_BOARD_LOCAL_RE = re.compile(r"^(jobs?-?noreply|jobalerts?(-noreply)?|job-?alerts?)$")

_NO_SENDER = {"", "unknown", "none", "n/a", "na", "-", "any", "?", "unknown.com"}

# Domain labels that say nothing about which site sent a mail.
_GENERIC_LABELS = {
    "com", "co", "net", "org", "io", "ai", "app", "in", "uk", "us", "jobs", "job",
    "careers", "career", "mail", "email", "emails", "www", "apply", "hr", "bank",
    "gov", "edu", "ac", "cloud", "notifications", "notification", "noreply",
    "no-reply", "auth", "login", "secure", "portal", "talent", "recruiting", "hire",
    "my", "info", "alerts", "news",
}
# ATS vendors whose sending domain differs from the domain the agent sees.
_LABEL_ALIASES = {
    "oraclecloud": ("oracle",),
    "myworkday": ("workday",),
    "myworkdayjobs": ("workday",),
    "workday": ("myworkday",),
}
# Hint words that never identify a site.
_HINT_STOPWORDS = {
    "verification", "verify", "verified", "code", "codes", "email", "emails", "mail",
    "request", "first", "second", "third", "final", "last", "account", "your",
    "application", "applications", "apply", "link", "links", "complete", "form",
    "sign", "signin", "login", "from", "with", "this", "that", "enter", "digit",
    "digits", "character", "characters", "alphanumeric", "full", "previous",
    "replies", "were", "truncated", "password", "passcode", "time", "onetime",
    "confirm", "confirmation", "identity", "security", "access", "authentication",
    "multi", "factor", "just", "sent", "resend", "again", "jobs", "careers",
    "career", "portal", "candidate", "process", "reply", "entry", "field", "only",
    "clickable", "have", "not", "number", "gmail", "bank",
    "team", "page", "click", "button", "login", "temporary", "expired", "fresh",
    "please", "code)", "otp", "pin", "send", "here", "will", "should", "inbox",
}


def _candidate_name_words() -> set[str]:
    """The candidate's own name never names a brand (personal/candidate.json)."""
    from policy import personal

    return {w for w in re.findall(r"[a-z0-9]+", personal.full_name().lower()) if w}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


# --- White-label ATS mail sent in the EMPLOYER's name --------------------------
# Job 1429 (Treyco, Oracle Cloud Recruiting), 2026-10-04 and 2026-10-11: the
# agent asked `NEED_OTP: oraclecloud.com | Verify it's you`. Oracle Fusion
# mailed the code as
#     From: Treyco-Fusion-Notification <Treyco-Fusion-Notification@treycoservice.com>
#     Subject: Confirm your identity
#     "... confirm your identity using the one-time pass code : <digits> ...
#      Sincerely, Treyco Talent Acquisition Team"
# and the word "Oracle" appears nowhere. The open pass found that mail on all
# 24 polls and skipped it every time ("neither sender headers nor body show
# the requested site"), so a job that did everything right was marked stuck.
# Oracle Fusion, Workday tenants, iCIMS, SuccessFactors, Taleo, Avature and
# friends all mail candidates from (or in the name of) the employer.
#
# So the employer named on the job counts as the requested site, but ONLY when
# the agent named one of these vendors. Reasoning, since a wrong code typed
# into a form is far worse than no code:
#   * A vendor request means "the site is hosted by a platform that speaks for
#     an employer". The employer of THIS job is then the expected sender, and
#     nobody else is. An Adatum mail on a Treyco job is still refused.
#   * When the agent named a non-vendor site (e.g. "paradox.ai" for Humongo is a
#     vendor, but "jobrelay.com" for a Northwind listing is a third party),
#     a Northwind code would belong to a different form than the one asking,
#     so employer evidence is NOT accepted there.
#   * When the agent named nothing, the search is already open; this changes
#     nothing.
#   * Every other guard runs first and is unchanged: the not_before/minutes
#     window, job-board and notification-mailbox rejection (even when the
#     company name appears in a LinkedIn "application sent to Treyco" mail), the
#     LinkedIn/Google refusal in agent_driver, and strict verification wording
#     next to the code on the unfiltered pass.
#   * Brand matching is token based, never substring: "Treyco" matches the word
#     "Treyco" and the domain label "treycoservice" (brand + a corporate suffix
#     word), never "excel"; "TT" never matches "http" or "ttl".
_WHITE_LABEL_ATS: tuple[str, ...] = (
    "oraclecloud", "oracle", "taleo", "myworkdayjobs", "myworkday", "workday",
    "icims", "successfactors", "sapsf", "avature", "paradox", "brassring",
    "kenexa", "phenompeople", "phenom", "eightfold", "smartrecruiters",
    "jobvite", "csod", "cornerstoneondemand", "ultipro", "ukg", "darwinbox",
    "peoplestrong", "zohorecruit", "turbohire", "lever", "greenhouse", "ashbyhq",
    "workable", "recruitee", "teamtailor", "bamboohr", "freshteam", "keka",
    "ceipal", "jazzhr", "pinpointhq", "personio", "adp", "talentrecruit",
)

# Words that say nothing about WHICH company a name is. Dropped from the
# company name before matching ("Treyco Service Holdings Pvt Ltd" -> "treyco").
_COMPANY_STOPWORDS = {
    "pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc",
    "corp", "corporation", "co", "company", "companies", "group", "holdings",
    "holding", "india", "in", "the", "of", "and", "for", "services", "service",
    "solutions", "solution", "technologies", "technology", "tech", "global",
    "international", "systems", "software", "consulting", "consultancy",
    "enterprises", "enterprise", "gmbh", "ag", "sa", "bv", "nv", "pte", "pty",
    "employer", "record", "careers", "career", "jobs", "hr", "talent", "team",
    "recruiting", "recruitment", "labs", "lab", "analytics", "digital", "c",
}
# Suffixes an employer glues onto its brand in a mail domain label:
# "treycoservice", "adatumcorp", "wiprocareers".
_DOMAIN_SUFFIX_WORDS = (
    "services", "service", "group", "global", "india", "inc", "corp", "tech",
    "technologies", "solutions", "careers", "career", "jobs", "hr", "talent",
    "recruiting", "mail", "notifications", "notification", "systems", "software",
    "consulting", "labs", "holdings", "co", "ltd", "online", "digital", "hq",
)
# Hosts that belong to the requesting ATS and whose first label is the
# employer's tenant: treyco.wd1.myworkdayjobs.com, careers-treyco.icims.com,
# treyco.taleo.net, treyco.avature.net, treyco.talentrecruit.com.
_TENANT_HOST_RES: tuple[tuple[str, re.Pattern], ...] = (
    ("workday", re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkday(?:jobs|site)\.com$")),
    ("icims", re.compile(r"^(?:careers-|jobs-)?([a-z0-9-]+)\.icims\.com$")),
    ("oracle", re.compile(r"^([a-z0-9-]+)\.taleo\.net$")),
    ("avature", re.compile(r"^([a-z0-9-]+)\.avature\.net$")),
    ("talentrecruit", re.compile(r"^([a-z0-9-]+)\.talentrecruit\.com$")),
)
# Vendor label -> family, so a tenant is only read from a page of the SAME
# platform the agent asked about (a leftover tab of another employer's
# Workday must not vouch for mail on an iCIMS job).
_VENDOR_FAMILY = {
    "myworkday": "workday", "myworkdayjobs": "workday", "myworkdaysite": "workday",
    "workday": "workday", "icims": "icims", "taleo": "oracle", "oracle": "oracle",
    "oraclecloud": "oracle", "avature": "avature", "talentrecruit": "talentrecruit",
}
_TENANT_STOP = {"www", "careers", "career", "jobs", "job", "apply", "login", "secure", "global"}
# Never an employer identity: words every vendor notification carries
# ("Treyco-Fusion-Notification", "workflow.mail...oracle.com"), and the account
# providers agent_driver refuses outright.
_NOT_A_BRAND = {
    "fusion", "workflow", "notification", "notifications", "noreply", "donotreply",
    "acquisition", "linkedin", "google", "gmail", "microsoft", "outlook", "sendgrid",
    "mailgun", "amazonses", "mailchimp", "sparkpost",
}


def _words(text: str) -> list[str]:
    import unicodedata

    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", t.lower().replace("'", "").replace("\u2019", ""))


def employer_brands(company: str) -> tuple[str, ...]:
    """Distinctive brand phrases for a company name, as space-joined words.

    "Treyco" -> ("treyco",); "Brian's India" -> ("brians",); "Harbor Point" ->
    ("harbor point",); "Northwind in India" -> ("northwind",). A name that is
    nothing but generic words ("Global Services Ltd") yields ().
    """
    words = [w for w in _words(company) if w not in _COMPANY_STOPWORDS]
    if not words:
        return ()
    phrase = " ".join(words)
    compact = "".join(words)
    if len(compact) < 2 or compact.isdigit() or compact in _GENERIC_LABELS:
        return ()
    board_labels = {d.split(".")[0] for d in JOB_BOARD_DOMAINS}
    if compact in _WHITE_LABEL_ATS or compact in board_labels or compact in _NOT_A_BRAND:
        # A vendor, job board or mail provider as the "employer" would
        # re-open exactly the traffic the guard exists to refuse.
        return ()
    return (phrase,)


def employer_tenants(urls, families=None) -> tuple[str, ...]:
    """Employer tenant names read from ATS page URLs (see _TENANT_HOST_RES).

    families: if given, only hosts of these vendor families count.
    """
    from urllib.parse import urlparse

    out: list[str] = []
    for url in urls or ():
        try:
            host = (urlparse(str(url)).hostname or "").lower()
        except ValueError:
            continue
        for family, rx in _TENANT_HOST_RES:
            if families is not None and family not in families:
                continue
            m = rx.match(host)
            if not m:
                continue
            tenant = re.sub(r"[^a-z0-9]", "", m.group(1))
            if (
                len(tenant) >= 3
                and not tenant.isdigit()
                and tenant not in _TENANT_STOP
                and tenant not in _GENERIC_LABELS
                and tenant not in _NOT_A_BRAND
            ):
                out.append(tenant)
    return tuple(dict.fromkeys(out))


# Job 1899, 2026-10-18: a TalentRecruit form at <tenant>.talentrecruit.com,
# tenant "qxrinnovation", employer "QXR Innovation Centre India". The code came
#     From: QXR India Recruiting <recruiting@qxr.com>
#     Subject: Candidate Verification Email
# five times, and every poll skipped it: the brand phrase is "qxr innovation
# centre", and neither "qxr.com" nor "QXR India Recruiting" carries it whole.
# The leading word alone ("qxr") is too weak on its own ("Harbor Point" must
# not vouch for harbor.com), but here a second, independent source spells the
# same start: the ATS tenant equals the name's first words run together. Only
# then does the leading word count as the employer.
def tenant_confirmed_lead(company: str, tenants) -> tuple[str, ...]:
    """The company's leading word, when an ATS tenant spells the name's start.

    "QXR Innovation Centre" + tenant "qxrinnovation" -> ("qxr",). A tenant that
    is only the first word adds nothing (it is already an employer identity),
    and a lead shorter than 3 letters or a generic word never counts.
    """
    words = [w for w in _words(company) if w not in _COMPANY_STOPWORDS]
    if len(words) < 2 or len(words[0]) < 3 or not employer_brands(words[0]):
        return ()
    starts = {"".join(words[:k]) for k in range(2, len(words) + 1)}
    if any(t in starts for t in tenants or ()):
        return (words[0],)
    return ()


def _label_is_brand(label: str, compact: str) -> bool:
    """Is a domain label the brand, alone or with a corporate suffix glued on?"""
    if label == compact:
        return True
    return label.startswith(compact) and label[len(compact):] in _DOMAIN_SUFFIX_WORDS


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc"}


def _registrable_labels(header_text: str) -> list[str]:
    """The registrable label of every mail address in sender headers.

    "a@mail.treycoservice.com" -> "treycoservice"; "x@careers.pcs.co.in" -> "pcs".
    Subdomains are ignored on purpose: "mg.substack.com" (a Mailgun bounce
    host) must not read as the employer "R&B", nor "codeharbor-x.devpost.com" as
    CodeHarbor.
    """
    out: list[str] = []
    for dom in re.findall(r"@([a-z0-9.-]+\.[a-z]{2,})", (header_text or "").lower()):
        parts = [p for p in dom.strip(".").split(".") if p]
        if len(parts) < 2:
            continue
        if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
            label = parts[-3]
        else:
            label = parts[-2]
        out.append(label.replace("-", ""))
    return out


def _display_words(from_hdr: str) -> list[str]:
    """Words of the From display name and address local part (not domain)."""
    words: list[str] = []
    for m in re.finditer(r"([^<,]*)<([^>]*)>|([^\s<>,]+@[^\s<>,]+)", from_hdr or ""):
        name, addr, bare = m.group(1) or "", m.group(2) or "", m.group(3) or ""
        local = (addr or bare).partition("@")[0]
        words.extend(_words(name))
        words.extend(_words(local))
    return words


def _has_phrase(words: list[str], phrase: str) -> bool:
    target = phrase.split()
    n = len(target)
    if n == 1:
        return target[0] in words
    return any(words[i : i + n] == target for i in range(len(words) - n + 1))


def _from_address(from_hdr: str) -> str:
    m = re.search(r"<([^>]+)>", from_hdr)
    addr = (m.group(1) if m else from_hdr).strip().lower()
    return addr if "@" in addr else ""


def _domain_re(domain: str) -> re.Pattern:
    # "xy.com" must match "@xy.com" / "careers.xy.com", never "galaxy.com".
    return re.compile(rf"(?<![a-z0-9-]){re.escape(domain)}(?![a-z0-9-])")


def _clean_sender(sender: str) -> str:
    s = (sender or "").strip().lower().strip("<>\"'` ")
    s = re.sub(r"^(from:|mailto:|https?://)", "", s)
    s = s.split("/")[0]
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    s = s.strip(". ")
    if s in _NO_SENDER or not re.search(r"[a-z]", s):
        return ""
    if s.startswith("localhost") or re.fullmatch(r"[\d.:]+", s):
        return ""
    return s


@dataclass
class SiteHint:
    """What the agent told us about the site that should be mailing a code."""

    sender: str = ""  # cleaned sender for the Gmail `from:` filter
    domains: tuple[str, ...] = ()  # e.g. ("humongo.paradox.ai", "paradox.ai")
    labels: tuple[str, ...] = ()  # e.g. ("humongo", "paradox")
    brands: tuple[str, ...] = ()  # words from the agent's hint, e.g. ("humongo",)
    # The job's employer, from the DB row and ATS tenant URLs, e.g. ("treyco",).
    # Only consulted when the agent named a white-label ATS vendor (see
    # _WHITE_LABEL_ATS above for why).
    employer: tuple[str, ...] = ()

    def _vendor_names(self) -> set[str]:
        names = set(self.labels) | set(self.brands)
        for d in self.domains:
            names.update(d.split("."))
        return names

    @property
    def vendor_named(self) -> bool:
        """Did the agent name a white-label ATS that mails as the employer?"""
        return any(n in _WHITE_LABEL_ATS for n in self._vendor_names())

    def with_employer(self, company: str = "", urls=()) -> "SiteHint":
        """This hint plus the job's employer identity (company name, tenants)."""
        found = list(employer_brands(company))
        families = {_VENDOR_FAMILY[n] for n in self._vendor_names() if n in _VENDOR_FAMILY}
        tenants = list(employer_tenants(urls, families))
        if self.sender:
            tenants.extend(employer_tenants([f"https://{self.sender}/"], families))
        found.extend(tenants)
        found.extend(tenant_confirmed_lead(company, tenants))
        self.employer = tuple(dict.fromkeys(f for f in found if f))
        return self

    def employer_matches(self, from_hdr: str, body: str) -> str:
        """The employer brand this mail visibly carries, or "".

        Evidence is taken from the SENDER only, never the body:
          * the brand as whole word(s) of the From display name or address
            local part ("Treyco" in "Treyco-Fusion-Notification", "Wren's Talent
            Attraction <donotreply@successfactors.com>"), or
          * the registrable domain of a From/Sender/Reply-To/Return-Path
            address, equal to the brand or the brand plus a corporate suffix
            ("treycoservice.com", "pcs.com"; subdomains such as mg.substack.com
            never count).
        The body is ignored on purpose: measured on the user's real inbox
        (2026-10-12, 400 mails x 870 DB companies), body links made
        newsletters "show" Apple (apps.apple.com), IBM and Heron
        (examplegroup.com), and company names are often ordinary words
        ("Canvas", "Anchor", "Compass"). Matching is by whole token, so "treyco"
        never matches "excel" and "tt" never matches "http" or "ttl".
        `body` is accepted for signature symmetry with SiteHint.matches.
        """
        if not self.employer or not self.vendor_named:
            return ""
        name_words = _display_words(from_hdr)
        reg_labels = _registrable_labels(from_hdr)
        for phrase in self.employer:
            compact = phrase.replace(" ", "")
            if _has_phrase(name_words, phrase) or compact in name_words:
                return phrase
            if any(_label_is_brand(lab, compact) for lab in reg_labels):
                return phrase
        return ""

    @property
    def named(self) -> bool:
        """True when the agent named a site: a domain, or a brand in its hint.

        Generic hint words ("Enter confirmation code for <email>") are dropped
        by _HINT_STOPWORDS, so such a hint names nothing and the unfiltered
        search stays open (job 1349, Zorvik). A stray non-generic word can
        make a real code unreachable; that fails safe (no code, never a wrong
        one).
        """
        return bool(self.domains or self.labels or self.brands)

    @classmethod
    def parse(cls, sender: str, hint: str = "") -> "SiteHint":
        s = _clean_sender(sender)
        domains: list[str] = []
        labels: list[str] = []
        if s:
            parts = [p for p in s.split(".") if p]
            if len(parts) >= 2:
                for i in range(len(parts) - 1):
                    if parts[i] in _GENERIC_LABELS and i > 0:
                        continue
                    d = ".".join(parts[i:])
                    if parts[i] not in _GENERIC_LABELS:
                        domains.append(d)
                body_labels = parts[:-1]
            else:
                body_labels = parts
            for lab in body_labels:
                if len(lab) >= 3 and lab not in _GENERIC_LABELS:
                    labels.append(lab)
                    labels.extend(_LABEL_ALIASES.get(lab, ()))
        hint_text = re.sub(r"\S+@\S+", " ", (hint or "").lower())
        hint_text = hint_text.replace("'", "").replace("\u2019", "")
        brands = [
            w
            for w in re.findall(r"[a-z0-9]+", hint_text)
            if len(w) >= 4 and not w.isdigit() and w not in _HINT_STOPWORDS
            and w not in _candidate_name_words()
        ]
        return cls(
            sender=s,
            domains=tuple(dict.fromkeys(domains)),
            labels=tuple(dict.fromkeys(labels)),
            brands=tuple(dict.fromkeys(brands)),
        )

    def names_board(self, board_domain: str) -> bool:
        """Did the agent ask for THIS job board's own code?"""
        board_label = board_domain.split(".")[0]
        return (
            board_label in self.labels
            or board_label in self.brands
            or any(d == board_domain or d.endswith("." + board_domain) for d in self.domains)
        )

    def matches(self, from_hdr: str, body: str) -> bool:
        """Does this mail visibly come from (or concern) the named site?

        from_hdr may carry several sender headers joined together (From,
        Sender, Reply-To, Return-Path), so a code relayed through an email
        provider the agent named still counts.
        """
        from_low = from_hdr.lower()
        from_norm = _norm(from_hdr)
        if any(lab in from_norm for lab in self.labels):
            return True
        body_low = (body or "").lower()
        for d in self.domains:
            rx = _domain_re(d)
            if rx.search(from_low) or rx.search(body_low):
                return True
        from_words = set(re.findall(r"[a-z0-9]+", from_low.replace("'", "").replace("\u2019", "")))
        return any(b in from_words for b in self.brands)


def _job_board_of(from_hdr: str) -> str:
    """The job board a mail comes from, or "" if it is not job-board mail."""
    addr = _from_address(from_hdr)
    if not addr:
        return ""
    local, _, domain = addr.partition("@")
    for board in JOB_BOARD_DOMAINS:
        if domain == board or domain.endswith("." + board):
            return board
    if _JOB_BOARD_LOCAL_RE.match(local):
        return domain
    return ""


def sender_rejection(from_hdr: str, body: str, site: SiteHint, other_senders: str = "") -> str:
    """Why this mail may NOT supply a code for `site`, or "" if it may.

    from_hdr: the From header. other_senders: Sender / Reply-To / Return-Path
    joined, used only as extra evidence that the mail came from the site.
    """
    local = _from_address(from_hdr).partition("@")[0]
    if _JOB_BOARD_LOCAL_RE.match(local):
        # Notification mailboxes (jobs-noreply@linkedin.com, jobalerts@...)
        # never send sign-in codes, even when the agent asked for that board.
        return f"job-alert/notification mailbox ({local})"
    board = _job_board_of(from_hdr)
    if board and not site.names_board(board):
        return f"job-board/notification sender ({board})"
    if site.named and not site.matches(f"{from_hdr} {other_senders}", body):
        # White-label ATS (Oracle Fusion, Workday, iCIMS, ...) mail in the
        # name of THIS job's employer. Job boards were refused above, so a
        # LinkedIn "application sent to Treyco" mail can never get here.
        if site.employer_matches(f"{from_hdr} {other_senders}", body):
            return ""
        if site.employer and site.vendor_named:
            return (
                "neither sender headers nor body show the requested site "
                f"or this job's employer ({', '.join(site.employer)})"
            )
        return "neither sender headers nor body show the requested site"
    return ""


# Workday serves forms from <tenant>.wdN.myworkdayjobs.com but mails from
# <tenant>@otp.workday.com (verification) or <tenant>@myworkday.com (receipts).
# A `from:myworkdayjobs.com` search finds neither (job 1523), leaving only the
# unfiltered pass, which sees just the newest 15 mails of the day.
_WORKDAY_MAIL_DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com", "workday.com")


def _from_filter(sender: str) -> str:
    """Gmail `from:` term for a cleaned sender, widened to the vendor's mail domains."""
    if any(sender == d or sender.endswith("." + d) for d in _WORKDAY_MAIL_DOMAINS):
        return "from:(" + " OR ".join(_WORKDAY_MAIL_DOMAINS) + ")"
    return f"from:{sender}"


def _build_query(*, sender: str, minutes: int, subject_hint: str) -> str:
    """Gmail search query. `newer_than` only supports d/m/y, so we filter
    precisely on the message's internalDate afterwards."""
    parts = ["newer_than:1d"]
    if sender:
        parts.append(_from_filter(sender))
    if subject_hint:
        cleaned = subject_hint.replace('"', " ").strip()
        if cleaned:
            parts.append(f'subject:"{cleaned}"')
    return " ".join(parts)


def _search_pass(
    service,
    *,
    query: str,
    minutes: int,
    max_messages: int,
    require_context: bool,
    site: Optional[SiteHint] = None,
    not_before: Optional[datetime] = None,
    loose_if_code_subject: bool = False,
    skip_ids: frozenset[str] = frozenset(),
) -> tuple[Optional[OtpResult], int]:
    """One Gmail search + scan. Returns (hit_or_None, messages_scanned).

    loose_if_code_subject: with require_context, still allow the loose
          extraction for a mail whose subject is about a code. Only used on
          the sender-filtered pass, where Gmail already limited the mail to
          the named site, so a templated code block ("Your code" subject, the
          digits alone in a styled box) is still readable.

    site: when given, every message must pass sender_rejection() before its
          code, sign-in link or blocker text is used.
    not_before: ignore mail received before this instant (the OTP request's
          start, minus a little slack for the send click that preceded it).
    skip_ids: Gmail message ids already handed to another job in a parallel
          run; never read twice.
    """
    try:
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_messages)
            .execute()
        )
    except Exception as e:
        return OtpResult(code=None, searched_query=query, error=f"gmail list failed: {e}"), 0

    messages = listing.get("messages", []) or []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    if not_before is not None and not_before > cutoff:
        cutoff = not_before
    scanned = 0

    for meta in messages:
        if meta.get("id") in skip_ids:
            continue
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=meta["id"], format="full")
                .execute()
            )
        except Exception as e:
            logger.debug("otp_lookup: could not fetch %s: %s", meta.get("id"), e)
            continue

        # Precise recency filter (Gmail's newer_than is day-granular).
        try:
            received = datetime.fromtimestamp(
                int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc
            )
        except (ValueError, TypeError):
            continue
        if received < cutoff:
            continue

        scanned += 1
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in msg.get("payload", {}).get("headers", []) or []
        }
        subject = headers.get("subject", "")
        from_hdr = headers.get("from", "")
        body = _walk_body(msg.get("payload", {}))

        if site is not None:
            others = " ".join(headers.get(k, "") for k in ("sender", "reply-to", "return-path"))
            why = sender_rejection(from_hdr, body, site, others)
            if why:
                logger.info(
                    "otp_lookup: skipped message from %s (subject=%r): %s",
                    from_hdr,
                    subject[:60],
                    why,
                )
                continue

        # Order matters. A code found next to explicit verification wording is
        # the strongest signal, so it wins outright. Otherwise a sign-in link
        # beats a loose shape-match, because link mails carry URLs stuffed
        # with digits: Goodfit's callback UUID "01a06b02-d103-7754-9d5c" was
        # being returned as the code "7754" (observed 2026-09-05).
        code = extract_code(body, subject=subject, require_context=True)
        if not code:
            link = extract_magic_link(body, subject=subject)
            if link:
                logger.info(
                    "otp_lookup: found sign-in link in message from %s (subject=%r)",
                    from_hdr,
                    subject[:60],
                )
                return (
                    OtpResult(
                        code=None,
                        magic_link=link,
                        sender=from_hdr,
                        subject=subject,
                        received_at=received.isoformat(),
                        searched_query=query,
                        candidates_scanned=scanned,
                        message_id=str(meta.get("id") or ""),
                    ),
                    scanned,
                )
            if not require_context or (
                loose_if_code_subject and _SUBJECT_CODE_WORD_RE.search(subject.lower())
            ):
                code = extract_code(body, subject=subject, require_context=False)
        if code:
            logger.info(
                "otp_lookup: found code in message from %s (subject=%r)", from_hdr, subject[:60]
            )
            return (
                OtpResult(
                    code=code,
                    sender=from_hdr,
                    subject=subject,
                    received_at=received.isoformat(),
                    searched_query=query,
                    candidates_scanned=scanned,
                    message_id=str(meta.get("id") or ""),
                ),
                scanned,
            )

        # No code - but the mail may explain why, in which case retrying is
        # pointless and the agent must be told to change tactic.
        haystack = f"{subject}\n{body}".lower()
        for pattern, guidance in NO_CODE_EXPLANATIONS:
            if re.search(pattern, haystack):
                logger.info(
                    "otp_lookup: no code, but sender explained why (%s): %r",
                    from_hdr,
                    subject[:60],
                )
                return (
                    OtpResult(
                        code=None,
                        sender=from_hdr,
                        subject=subject,
                        received_at=received.isoformat(),
                        searched_query=query,
                        candidates_scanned=scanned,
                        message_id=str(meta.get("id") or ""),
                        blocker=guidance,
                        error="no code sent; the portal replied with an explanation",
                    ),
                    scanned,
                )

    return None, scanned


def find_code(
    *,
    sender: str = "",
    minutes: int = 10,
    subject_hint: str = "",
    max_messages: int = 15,
    not_before: Optional[datetime] = None,
    company: str = "",
    employer_urls: tuple[str, ...] = (),
    skip_ids: frozenset[str] = frozenset(),
) -> OtpResult:
    """Search recent mail for a verification code. Single pass, no waiting.

    sender: substring/domain filter, e.g. "northwind.com". Treated as a HINT,
            not a hard requirement: real ATS codes frequently arrive from an
            email-service-provider domain (sendgrid.net, mail.icims.com,
            azurecomm.net) that no agent could predict from the page URL. So if
            the filtered search finds nothing, this retries WITHOUT the sender
            filter but demands explicit verification wording next to the code
            (require_context), which is what keeps an unrelated number from
            being mistaken for a code.
    minutes: only consider mail received within this many minutes (default 10).
    not_before: also ignore mail received before this instant. The apply
            driver passes the start of the agent turn that clicked "send
            code", so a code from an earlier request or an earlier job (1005
            and 1007 were both Humongo, back to back) can never be reused.
    company / employer_urls: the job's employer name (DB company_name) and
            any URLs of its application pages. When the agent named a
            white-label ATS (oraclecloud.com, myworkday.com, icims.com, ...),
            mail that visibly carries this employer counts as the requested
            site, because those platforms mail candidates in the employer's
            name (job 1429: Treyco's Oracle Fusion code came from
            Treyco-Fusion-Notification@treycoservice.com). Never widens anything
            when the agent named a non-vendor site or no site at all.

    Passes, in order (each later pass only runs if the earlier found nothing):
      1. sender + subject hint, loose extraction (unchanged behaviour)
      2. sender only, strict context. The agent's subject hint is usually a
         guess ("Humongo verification code") and must not hide the real mail.
      3. no Gmail filter, strict context
    Every pass applies sender_rejection(): job-board senders are never
    trusted unless the agent asked for that board, and when the agent named a
    site the mail has to show it. No code beats a wrong code.
    """
    site = SiteHint.parse(sender, subject_hint).with_employer(company, employer_urls)
    query = _build_query(sender=site.sender, minutes=minutes, subject_hint=subject_hint)
    try:
        service = _gmail_service()
    except Exception as e:
        return OtpResult(code=None, searched_query=query, error=str(e))

    total_scanned = 0
    blocker = ""

    # (query, require_context, loose_if_code_subject)
    # Loose extraction only when Gmail itself restricted the search to the
    # named sender; a subject-only filter ("NEED_OTP: unknown | ...") can
    # match anyone's mail.
    passes: list[tuple[str, bool, bool]] = [(query, not site.sender, False)]
    if site.sender and subject_hint:
        # Same sender, guessed subject dropped. Loose only for mail whose
        # subject is about a code: the sender's other mail (e.g. a Paradox
        # "complete your application" mail quoting requisition P25-330706-1)
        # must not yield a shape-match.
        passes.append(
            (_build_query(sender=site.sender, minutes=minutes, subject_hint=""), True, True)
        )
    if site.sender or subject_hint:
        passes.append((_build_query(sender="", minutes=minutes, subject_hint=""), True, False))

    for i, (q, strict, loose_subj) in enumerate(passes):
        if i:
            logger.info(
                "otp_lookup: no match for %r; retrying with %r (strict context matching).",
                passes[i - 1][0],
                q,
            )
        hit, scanned = _search_pass(
            service,
            query=q,
            minutes=minutes,
            max_messages=max_messages,
            require_context=strict,
            site=site,
            not_before=not_before,
            loose_if_code_subject=loose_subj,
            skip_ids=skip_ids,
        )
        total_scanned += scanned
        if hit is None:
            continue
        hit.candidates_scanned = total_scanned
        if hit.found or hit.magic_link or hit.blocker:
            return hit
        # Hard API error - surface it rather than retrying blindly.
        return hit

    return OtpResult(
        code=None,
        searched_query=query,
        candidates_scanned=total_scanned,
        blocker=blocker,
        error="no verification code found in the time window",
    )


def wait_for_code(
    *,
    sender: str = "",
    minutes: int = 10,
    subject_hint: str = "",
    timeout_sec: int = 90,
    poll_interval_sec: int = 6,
    not_before: Optional[datetime] = None,
    company: str = "",
    employer_urls: tuple[str, ...] = (),
    skip_ids=None,
) -> OtpResult:
    """Poll until a code shows up or the timeout expires.

    The agent's normal flow is: click "send verification code", then call
    this. The email usually lands within a few seconds but can take ~30s on
    slower ATS mailers, so a single find_code() call would frequently race it.
    not_before, company, employer_urls: see find_code().
    skip_ids: a callable returning the Gmail ids other jobs already consumed,
    re-read on every poll (a parallel run's ledger), or None.
    """
    deadline = time.monotonic() + timeout_sec
    last = OtpResult(code=None, error="timed out before any lookup ran")
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        last = find_code(
            sender=sender,
            minutes=minutes,
            subject_hint=subject_hint,
            not_before=not_before,
            company=company,
            employer_urls=employer_urls,
            skip_ids=frozenset(skip_ids()) if skip_ids else frozenset(),
        )
        if last.found or last.magic_link:
            return last
        # A definitive explanation ("account already exists", rate-limited)
        # will not change by waiting. Bail out immediately instead of burning
        # the full timeout, which is what happened 5 times on 2026-09-02.
        if last.blocker:
            logger.info("otp_lookup: stopping early, portal explained no code is coming.")
            return last
        # A hard credential/API error will not fix itself by polling.
        if last.error and "gmail" in last.error.lower() and "not found" not in last.error.lower():
            if "no verification code" not in last.error:
                return last
        time.sleep(poll_interval_sec)
    last.error = last.error or f"no code arrived within {timeout_sec}s"
    return last
