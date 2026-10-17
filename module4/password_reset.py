"""Password-reset handoff: retrieve a reset LINK from Gmail, driver-side.

Why this exists
---------------
The apply agent already has email access, so it can reset an unknown
password itself. One fixed
password, kept in the candidate's local config (personal/secrets.json or
JOBO_ACCOUNT_PASSWORD), never in the code.

On the 2026-09-03 run, 10 of 28 failures were the same wall: an ATS portal
where an account already exists for the candidate's email and the password is
unknown, so neither sign-up nor sign-in works. Blocking Northwind removed most
of them, but the wall itself still hits companies the user actually wants (Hazelton
SuccessFactors, Adatum Workday, and any portal he registered with once).

The pipeline already holds Gmail access for the OTP handoff. A password reset
is the same shape as an OTP with one difference: the mail carries a LINK rather
than a code, so this module extracts URLs instead of digits.

Design notes
------------
- **Links are ranked, not guessed.** A reset email contains many URLs (logo,
  unsubscribe, privacy, help centre). Scoring by reset-ish tokens in the URL
  plus proximity to reset wording in the text beats "take the first link",
  which in testing reliably picked a tracking pixel or the company homepage.
- **Domain must match.** A reset link is only returned if its host is related
  to the domain the agent asked about. Handing the agent a reset link for the
  WRONG service would be a security problem, not just a bug.
- **Recency window.** Only mail newer than `minutes` counts, so a stale reset
  link from a previous run cannot be replayed into a fresh flow. Same rule the
  OTP path learned the hard way.
- The agent never sees the mailbox: it prints a directive, the driver performs
  the lookup and returns exactly one URL. Same approval-gate shape as NEED_OTP.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote, urlsplit

from module4 import otp_lookup
from policy import personal

logger = logging.getLogger(__name__)

# The password every reset performed by this pipeline sets. Fixed and known so
# a later run can simply SIGN IN instead of resetting again.
# Read from personal/secrets.json (policy/personal.py). Never log it.
STANDARD_PASSWORD = personal.account_password()

# Tokens that mark a URL as the actual reset action rather than boilerplate.
_RESET_URL_TOKENS = (
    "reset",
    "resetpassword",
    "reset-password",
    "forgot",
    "setpassword",
    "set-password",
    "newpassword",
    "new-password",
    "changepassword",
    "change-password",
    "credential",
    "recover",
    "passwordreset",
    "updatepassword",
    "verifyemail",
    "activate",
    "token=",
    "otl",  # one-time-login, SuccessFactors
)

# Boilerplate links that appear in nearly every transactional email and must
# never be handed back as "the reset link".
_JUNK_URL_TOKENS = (
    "unsubscribe",
    "privacy",
    "cookie",
    "terms",
    "help",
    "support",
    "contact",
    "twitter.com",
    "facebook.com",
    "linkedin.com/company",
    "instagram.com",
    "youtube.com",
    "apple.com",
    "play.google.com",
    "/logo",
    ".png",
    ".jpg",
    ".gif",
    ".svg",
    "mailto:",
)

# Wording that identifies a mail as a password reset at all.
_RESET_SUBJECT_HINTS = (
    "reset",
    "password",
    "forgot",
    "recover",
    "credential",
    "account access",
)

_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+", re.I)


@dataclass
class ResetLinkResult:
    url: Optional[str] = None
    subject: str = ""
    sender: str = ""
    error: str = ""
    searched_query: str = ""
    candidates_seen: int = 0
    password: str = STANDARD_PASSWORD
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.url)


def _registrable(host: str) -> str:
    """Crude registrable-domain reduction, good enough for relatedness checks."""
    host = (host or "").lower().strip().lstrip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    # Handle the common two-part public suffixes we actually meet.
    two = ".".join(parts[-2:])
    if two in {"co.uk", "co.in", "com.au", "co.jp", "com.br", "co.za"}:
        return ".".join(parts[-3:])
    return two


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def domains_related(asked: str, link_host: str) -> bool:
    """True when a reset link plausibly belongs to the service asked about.

    Deliberately permissive about SUBDOMAINS and identity providers (an SAP
    portal mails from successfactors.eu, Workday from myworkday.com) but strict
    about unrelated registrable domains - handing back someone else's reset
    link would be a security bug.
    """
    asked_host = _host_of(asked) or asked.lower().strip()
    a = _registrable(asked_host)
    b = _registrable(link_host)
    if not a or not b:
        return False
    if a == b:
        return True
    # Brand-token relatedness, for identity providers on their own domain
    # (an SAP portal mails from successfactors.eu, Workday from myworkday.com).
    # The token must appear as a whole DOT-OR-HYPHEN separated label, never as
    # a bare substring: "notrippling-phish.com" contains "rippling" and must
    # NOT be treated as related. Handing back a lookalike domain's reset link
    # would be a phishing vector, not merely a bug.
    def labels(host: str) -> set[str]:
        parts = re.split(r"[.\-_]", host or "")
        return {p for p in parts if len(p) >= 5}

    a_labels = labels(asked_host)
    b_labels = labels(link_host)
    return bool(a_labels & b_labels)


def score_url(url: str, context: str = "") -> int:
    """Rank a candidate URL by how much it looks like THE reset action."""
    u = unquote(url).lower()
    if any(j in u for j in _JUNK_URL_TOKENS):
        return -100
    score = 0
    for tok in _RESET_URL_TOKENS:
        if tok in u:
            score += 10
    # A long opaque token in the query string is the hallmark of a one-time
    # reset link.
    if re.search(r"[?&](token|code|key|t|otl|guid|nonce)=[A-Za-z0-9._%-]{12,}", u):
        score += 25
    elif re.search(r"/[A-Za-z0-9._-]{24,}(/|$)", u):
        score += 12
    if context:
        score += 5
    # Prefer deeper action URLs over a bare homepage.
    path = urlsplit(u).path or "/"
    if path in ("", "/"):
        score -= 15
    return score


def extract_reset_url(text: str, *, expect_domain: str = "") -> tuple[Optional[str], int]:
    """Pull the best reset link out of an email body.

    Returns (url_or_None, candidates_seen).
    """
    if not text:
        return None, 0
    urls: list[str] = []
    seen = set()
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:)]}>\"'")
        if u in seen:
            continue
        seen.add(u)
        urls.append(u)
    if not urls:
        return None, 0

    lowered = text.lower()
    best, best_score = None, 0
    for u in urls:
        if expect_domain and not domains_related(expect_domain, _host_of(u)):
            continue
        # Context bonus: reset wording within ~200 chars before the link.
        idx = lowered.find(u.lower())
        ctx = lowered[max(0, idx - 200) : idx] if idx >= 0 else ""
        has_ctx = any(w in ctx for w in ("reset", "password", "click", "set up", "recover"))
        s = score_url(u, "reset" if has_ctx else "")
        if s > best_score:
            best_score, best = s, u
    # Require a positive, meaningful score - never return a link just because
    # it was the only one present.
    if best and best_score >= 10:
        return best, len(urls)
    return None, len(urls)


def find_reset_link(
    *,
    domain: str,
    minutes: int = 15,
    max_messages: int = 15,
) -> ResetLinkResult:
    """Search Gmail for a recent password-reset email for `domain`."""
    query = "newer_than:1d (subject:password OR subject:reset OR subject:verify)"
    try:
        service = otp_lookup._gmail_service()
    except Exception as e:
        return ResetLinkResult(error=f"gmail unavailable: {e}", searched_query=query)

    try:
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_messages)
            .execute()
        )
    except Exception as e:
        return ResetLinkResult(error=f"gmail list failed: {e}", searched_query=query)

    messages = listing.get("messages", []) or []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    scanned = 0

    for meta in messages:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=meta["id"], format="full")
                .execute()
            )
        except Exception as e:
            logger.debug("password_reset: could not fetch %s: %s", meta.get("id"), e)
            continue

        try:
            received = datetime.fromtimestamp(
                int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc
            )
        except (ValueError, TypeError):
            continue
        if received < cutoff:
            continue

        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        scanned += 1

        if not any(h in subject.lower() for h in _RESET_SUBJECT_HINTS):
            continue

        body = otp_lookup._walk_body(msg.get("payload", {}))
        body = otp_lookup._strip_html(body)
        url, n = extract_reset_url(body, expect_domain=domain)
        if url:
            logger.info(
                "password_reset: found a reset link for %s in %r (%d candidate URLs)",
                domain,
                subject[:60],
                n,
            )
            return ResetLinkResult(
                url=url,
                subject=subject,
                sender=sender,
                searched_query=query,
                candidates_seen=n,
            )

    return ResetLinkResult(
        error=(
            f"no password-reset email for {domain} in the last {minutes} minutes "
            f"({scanned} recent message(s) checked)"
        ),
        searched_query=query,
        candidates_seen=scanned,
    )


# `NEED_PASSWORD_RESET: <domain>`
_REQUEST_RE = re.compile(
    r"^NEED_PASSWORD_RESET:\s*(?P<domain>\S+)\s*$", re.MULTILINE | re.I
)


def parse_request(text: str) -> Optional[str]:
    m = _REQUEST_RE.search(text)
    return m.group("domain").strip() if m else None
