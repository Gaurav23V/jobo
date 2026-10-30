"""Run one application attempt by driving a Hermes one-shot agent session.

The agent gets Hermes's native browser toolset (browser_navigate,
browser_snapshot, browser_click, ...) attached via BROWSER_CDP_URL to our
persistent, LinkedIn-signed-in Chromium instance (module4/browser_session.py
manages that process). This replaces the old approach of shelling out to the
standalone agent-browser CLI through the terminal toolset - same underlying
CDP protocol, but native tool calls instead of terminal-invocation-plus-
output-parsing, and we get browser_cdp's DOM.setFileInputFiles for uploads,
browser_dialog for JS confirm()/alert() popups, and frame-aware snapshots for
free.
"""

from __future__ import annotations

import logging
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from module4 import captcha_walls, email_apply, submission_audit
from policy import made_up_values, personal

logger = logging.getLogger(__name__)

# First attempt gets the full budget to figure out a novel ATS from scratch.
# Retries get less ONLY when the previous failure was not budget-related: if
# the same job failed for a structural reason, more wall-clock rarely fixes it
# and a different tactic (informed by the previous failure detail baked into
# the retry prompt) is what helps.
#
# When the previous failure WAS budget exhaustion, the budget escalates
# instead - see AGENT_STEPS_ESCALATED. Evidence from the 2026-09-02 and
# 2026-09-03 runs: jobs kept dying at 90-95% of a long form (job462 attached
# the resume then ran out on text fields; job393 filled resume, personal info
# and salary, then ran out on the final questions step). Those jobs need room,
# not a different tactic.
AGENT_TIMEOUT_FIRST_SEC = 1200  # 20 min
AGENT_TIMEOUT_RETRY_SEC = 480  # 8 min
AGENT_TIMEOUT_ESCALATED_SEC = 1800  # 30 min, for a budget-starved retry
# Step budgets are TOTAL model calls for the whole job. Until 2026-09-28 they
# were passed as `hermes --max-turns`, which Hermes applies per run, and every
# NEED_* handoff starts a new run with a fresh allowance - so one job made 232
# model calls against a nominal 60 (job489, 2026-09-11). The driver now reads
# the session's call count after every round and resumes with only what is
# left. Replaying the 100 sessions since 2026-09-11: a real cap of 80 cuts
# ~45% of apply tokens and would have stopped 2 of 20 successful jobs early.
AGENT_STEPS_FIRST = 80
AGENT_STEPS_RETRY = 50
AGENT_STEPS_ESCALATED = 100
VALID_STATUSES = {
    "submitted",
    "closed",
    "captcha_failed",
    "stuck",
    "already_applied",
    # The agent discovered mid-application that the apply flow routes through a
    # blocklisted destination (e.g. an "Apply on company website" button that
    # lands on internshala.com). This is a REFUSAL, not a failure: the job is
    # dropped permanently and no attempt is charged.
    "blocked_employer",
    # The flow landed on a temporarily paused ATS (policy/paused_sites.py).
    # Not a failure either: no attempt is charged and the job comes back once
    # the site is un-paused.
    "paused_site",
    # A captcha the agent never passes: hCaptcha on the email/sign-in step of
    # an Oracle Cloud or iCIMS site or anywhere on a Paycom / Lever site, or
    # Cutshort's Cloudflare Turnstile (module4/captcha_walls.py). Only the plugin's notice asks for it; the
    # job is parked, no attempt charged.
    "captcha_wall",
    # LinkedIn shows the shared apply browser as signed out. A PIPELINE
    # problem: the runner stops the stage and charges no attempt
    # (module4/linkedin_session.py). The agent never signs in itself.
    "linkedin_signed_out",
    # The posting has no web form, only "email your CV to ..." (a mailto
    # link). The driver writes and sends that email (module4/email_apply.py);
    # the agent only reports the link on an EMAIL_APPLY: line.
    "email_apply",
}
STATUS_EMAIL_APPLY = email_apply.STATUS
EMAIL_APPLY_RE = re.compile(r"^EMAIL_APPLY:\s*(\S.*?)\s*$", re.MULTILINE)

# --- Emailed-verification-code handoff -------------------------------------
# 7 of 19 apply failures on the 2026-09-01 run were ATS portals that gate the
# application form behind a 6-digit code emailed to the candidate (Northwind/Azure B2C,
# Adatum Workday, Shine, SuccessFactors, TurboHire). The agent could fill
# every field but had no way to read the inbox.
#
# The agent is NOT given Gmail access to solve this. Instead it prints a
# directive and pauses; THIS driver performs the narrow lookup
# (module4.otp_lookup, restricted to a fresh time window and a sender filter)
# and resumes the same agent session with just the code injected. The agent
# therefore never holds mail credentials, never sees message bodies, and has
# no verb for reading anything else - the approval-gate pattern, with the
# driver as the gate. Browser state survives because resume reattaches to the
# same persistent CDP browser.
OTP_REQUEST_RE = re.compile(
    r"^NEED_OTP:\s*(?P<sender>[^\s|]+)(?:\s*\|\s*(?P<hint>.+))?\s*$",
    re.MULTILINE,
)
# --- Deterministic file-upload handoff -------------------------------------
# Uploads were the largest failure bucket two runs running (9/19, then 15/28).
# On 2026-09-02 the correct CDP technique was proven by experiment and written
# into prompts.py; the very next run job393 failed on the same shadow-DOM input
# anyway, with the agent reaching for the objectId route the prompt warns
# against. Prompt guidance loses to model habit on mechanical DOM work, so the
# upload now happens in code (module4/uploader.py) and the agent only names
# which slot it wants. Same shape as the OTP handoff, which works in production.
UPLOAD_REQUEST_RE = re.compile(
    r"^NEED_UPLOAD:\s*(?:resume|cover|cover_letter|cover-letter|cv)?\s*(?:\|.*)?$",
    re.MULTILINE | re.I,
)
# --- Deterministic form-field handoff --------------------------------------
# Once uploads were fixed, select2-style autocompletes became the dominant
# turn sink: on the 2026-09-04 run 5 of 26 stuck jobs died on them, each
# burning the WHOLE budget on a single field. A transcript named the cause:
# "clicked the dropdown without registering the selection - the underlying
# hidden <select id='10955'> never updated". Same lesson as uploads: the
# mechanical DOM work moves into code (module4/form_filler.py).
# Horizontal whitespace only: an empty assignment must not absorb stderr or
# a following STATUS/session_id line. Keep in sync with form_filler.parse_request.
FIELD_REQUEST_RE = re.compile(
    r"^[ \t]*NEED_FIELD:[ \t]*[^ \t\r\n=][^\r\n=]*?[ \t]*=[ \t]*[^\r\n]*\r?$",
    re.MULTILINE | re.I,
)
# `NEED_FIELDS` (no colon needed) asks for a NUMBERED list of the page's
# controls. Numbers remove the matching problem entirely - see the module
# docstring in form_filler.py for the wrong-field write this prevents.
FIELDS_LIST_RE = re.compile(r"^NEED_FIELDS\b.*$", re.MULTILINE | re.I)
# `NEED_PASSWORD_RESET: <domain>` - the driver finds the reset link in Gmail
# and hands back the link plus the standard password to set. 10 of 28 failures
# on the 2026-09-03 run were "account exists, password unknown" walls.
PASSWORD_RESET_RE = re.compile(r"^NEED_PASSWORD_RESET:\s*\S+\s*$", re.MULTILINE | re.I)
# `NEED_CLICK: <label>` - the driver scrolls the button into view, clears any
# overlay covering it, clicks with real pointer events and verifies the page
# actually changed. Two jobs on the 2026-09-05 run filled every field and then
# died on an overlay-blocked Submit button.
CLICK_REQUEST_RE = re.compile(r"^NEED_CLICK:?\s*.*$", re.MULTILINE | re.I)
# `NEED_SLIDER` - the driver solves an on-page jigsaw "Slide to verify"
# captcha in code (module4/slider_captcha.py). Redpine 1485 (2026-10-11) spent
# 8.6 min eyeballing the gap and failed; in code it is about 5 s.
SLIDER_REQUEST_RE = re.compile(r"^NEED_SLIDER\b.*$", re.MULTILINE | re.I)
SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)\s*$", re.MULTILINE)
# A single form rarely needs more than one code; allow a couple of rounds for
# portals that verify email AND phone, but cap it so a confused agent asking
# for codes in a loop cannot burn the whole job budget.
MAX_OTP_ROUNDS = 3
OTP_WAIT_SEC = 90
OTP_WINDOW_MINUTES = 10
OTP_CLOCK_SLACK_SEC = 60
# Resume + cover letter + a retry each is the realistic ceiling.
MAX_UPLOAD_ROUNDS = 4
# "Already attached" answers (the site already shows that file; nothing was
# uploaded) are not charged against MAX_UPLOAD_ROUNDS, up to this many.
UPLOAD_ALREADY_PREFIX = "UPLOAD_DONE: ALREADY_ATTACHED"
# module4/uploader.py's answer when no tab of this browser has a file input.
NO_FILE_INPUT = "no <input type=file> found"
MAX_ALREADY_REFUNDS = 3
# Long ATS forms legitimately have many awkward dropdowns; this is a
# safety net against a looping agent, not a budget.
MAX_FIELD_ROUNDS = 25
# A set that fills a field not set before is progress, not a loop, so it is
# not charged to MAX_FIELD_ROUNDS (up to this many). Larkspur 3015 (Oracle,
# 2026-10-30) hit the cap on its 16th straight yes/no answer, with each one
# set first time, and the rest of the page had to be done by hand.
MAX_NEW_FIELD_REFUNDS = 60
# Per-FIELD strike limit. The global cap above is far too loose to stop the
# real failure: one stubborn control eating an entire budget (job99997 spent
# nine rounds on a single college dropdown). Three honest attempts at one
# field, then the agent is told to move on and submit what it has.
MAX_FAILURES_PER_FIELD = 3
# Listing is cheap and idempotent, but cap it so a confused agent cannot
# re-list forever instead of filling anything.
MAX_FIELD_LIST_ROUNDS = 8
# form_filler's note when a listed number now points at a different control
# (and the listed control cannot be found again by its signature). Nothing was
# typed, so the set is not charged to MAX_FIELD_ROUNDS or the field's strikes;
# the rest of that batch is held back and the driver re-lists the form itself.
# Kestrel 1622/1628 (2026-10-15) burned the whole 25-set cap this way. The
# refunds and driver re-lists are bounded so a form that never settles still
# gets cut off.
FORM_CHANGED_NOTE = "the form changed since the fields were listed"
FIELD_MOVED_NOTE = "the form changed and this field moved to #"
MAX_STALE_REFUNDS = 8
# A reset is: request the mail, wait, open the link, set the password. Two
# rounds covers a resend; more means the flow is not working.
MAX_PASSWORD_RESET_ROUNDS = 2
PASSWORD_RESET_WAIT_SEC = 75
# Account setup (sign in, create account, forgot password, reset email, an
# emailed code or link) comes before the form and used to come out of the
# form's budget. 1464 Willowby
# (2026-10-15) spent its first ~10 minutes on Workday sign-in clicks and a
# reset, then ran out at 80/80 on My Information, step 1 of 5. The first
# account handoff in a job adds this much room, once (GHO-20).
ACCOUNT_SETUP_STEPS = 25
ACCOUNT_SETUP_SEC = 360
_ACCOUNT_SETUP_CLICK = re.compile(
    r"\b(?:sign[ -]?in|log[ -]?in|sign[ -]?up|create (?:an? |your )?account|"
    r"register|forgot|reset (?:your )?password|verify (?:your )?email)\b",
    re.I,
)
# A long wizard that is still moving gets one more allowance near the end.
# Wingtip 1594 (Avature, 2026-10-24) set 17 fields and attached its files on two
# pages, then ran out at 80/80 five answers into the per-country work
# authorization page; three other Wingtip jobs did the same form in 33-48 steps.
# Moving means the pipeline itself set PROGRESS_MIN_NEW_FIELDS fields it had
# not set before within the last PROGRESS_WINDOW_STEPS steps, so an agent
# re-setting one field in a loop never qualifies. Granted once per job, when
# a handoff finds PROGRESS_LOW_WATER steps or fewer left.
PROGRESS_EXTENSION_STEPS = 25
PROGRESS_EXTENSION_SEC = 360
PROGRESS_MIN_NEW_FIELDS = 3
PROGRESS_WINDOW_STEPS = 20
PROGRESS_LOW_WATER = 10
# A long form gets room for its length, once, when a field listing first
# shows LONG_FORM_CONTROLS or more. Liberty 3048 (Paylocity, 91 controls) ran out
# at 80/80 and again at 50/50 on its retry, and Consolidated 2802 (LinkedIn, 209
# controls) at 80/80, both mid-form (2026-10-30, GHO-107). A retry is lifted
# to the first-try budget first: the 50-step retry budget was set for forms
# that failed on something other than their length.
LONG_FORM_CONTROLS = 40
LONG_FORM_STEPS = 30
LONG_FORM_SEC = 360
# A paged listing names the page's total ("#1-#60 of 91 controls"); a short
# one only counts what it lists, which is all of them.
_PAGED_TOTAL_RE = re.compile(r"\bof (\d+) controls\b")
_LISTED_COUNT_RE = re.compile(r"^FIELDS_LISTED: (\d+)\b", re.MULTILINE)
# `NEED_FIELDS #61` lists the page of controls from #61 on.
_LIST_START_RE = re.compile(r"#\s*(\d+)")
_FIELD_SET_LABEL_RE = re.compile(r"^FIELD_SET: (.+?) = ", re.MULTILINE)
# Clicking is cheap, but a runaway loop of Submit clicks risks duplicate
# applications, so keep this tight.
MAX_CLICK_ROUNDS = 5
# Each round already makes up to 3 drags; a second round covers a puzzle the
# site redraws after a failed submit.
MAX_SLIDER_ROUNDS = 2

# Native browser + vision only. The old design granted the full hermes-cli
# toolset (terminal, file, memory, cronjob, delegation, image_gen, tts, ...)
# because the agent-browser CLI needed `terminal` to run shell commands; none
# of that is needed anymore, and a narrower toolset means fewer tokens spent
# describing tools the agent will never use, and no temptation to reach for a
# shell as an alternate (slower, error-prone) path to the same DOM actions.
APPLY_AGENT_TOOLSETS = "browser,vision"

# Sent back when the agent asks the pipeline to help it sign in to LinkedIn or
# Google (an emailed security PIN, a password reset). Those are the user's own
# accounts; signing in is never the agent's job. 2026-10-03 job 1007.
_ACCOUNT_SIGNIN_REFUSAL = (
    "the pipeline never retrieves LinkedIn or Google sign-in codes, PINs or reset "
    "links - those are the user's own accounts and only he signs in to them. If LinkedIn "
    "shows this browser as signed out, stop and end with "
    "'STATUS: linkedin_signed_out' (DETAIL: LinkedIn shows the apply browser as "
    "signed out). The run stops and this job is not charged an attempt."
)

DEBUG_LOG_DIR = Path(__file__).parent.parent / "data" / "logs" / "apply_debug"


@dataclass
class ApplyOutcome:
    status: str
    detail: str
    fields_filled: dict = None  # type: ignore[assignment]
    # Resume evidence for the audit record (TODO item 18): the file name this
    # job's resume was uploaded under, how many verified resume uploads the
    # driver made, and whether an Easy Apply modal was seen / had our card
    # verified as the selected one.
    resume_upload_name: str = ""
    resume_uploads_ok: int = 0
    easy_apply_seen: bool = False
    easy_apply_resume_verified: bool = False
    # A submit gate (NEED_CLICK or the plugin's veto) saw this job's own file
    # on the form: e.g. a resumed Workday draft that kept an earlier upload.
    resume_seen_on_page: bool = False
    resume_rule_violation: str = ""
    # NEED_UPLOAD answers that found no file input in any tab of this
    # worker's browser. With no successful upload either, that is the mark of
    # an agent driving some other browser (GHO-17), which the runner counts.
    upload_no_file_input: int = 0
    # email_apply: the posting's mailto link as the agent read it.
    email_mailto: str = ""

    def __post_init__(self):
        if self.fields_filled is None:
            self.fields_filled = {}


# --- This job's resume on LinkedIn Easy Apply (TODO item 18) ----------------
# 13 of 27 sends on 2026-10-11 went out through Easy Apply with LinkedIn's
# saved, pre-selected (old) resume and zero uploads; every one of them used
# `NEED_CLICK: Submit application`. Three layers stop that now:
#   1. the prompt tells the agent to always NEED_UPLOAD on Easy Apply's resume
#      step, and each job uploads a uniquely named copy of its own resume
#      (module4/upload_names.py) so its card can be told apart from saved ones;
#   2. the NEED_CLICK gate below refuses a submit-like click while an Easy
#      Apply modal's selected resume is not this job's file (driver side, no
#      model cooperation needed);
#   3. the jobo-browser plugin's resume_guard vetoes a direct browser_click /
#      key / script submit in the same state (Hermes pre_tool_call).
# Both 2 and 3 use hermes_plugin/jobo-browser/resume_guard.py's probe + rule.
# GHO-70: 2 and 3 cover every other site too (judge_external): Trey 272 and
# Margie 1635 were submitted after UPLOAD_FAILED with nothing verified.
# Blank = "the submit button". "Apply"/"Send" too: harmless when no Easy
# Apply modal is open (the gate then allows), and a label like "Apply" must
# not slip past it while one is.
_SUBMITISH_CLICK_RE = re.compile(r"^\s*$|\bsubmit\b|\bsend\b|\bapply\b|\bfinish\b", re.I)
MAX_RESUME_REFUSALS = 3


class _JobContext:
    """Per-job state the upload/click resolvers share with run_apply_agent."""

    def __init__(self, job_id: int = 0, upload_name: str = "", state_file: str = ""):
        self.job_id = job_id
        self.upload_name = upload_name
        self.state_file = state_file
        self.resume_uploads_ok = 0
        self.easy_apply_seen = False
        self.easy_apply_verified = False
        self.upload_no_file_input = 0
        # Last UPLOAD_FAILED of this job's resume while no upload of it has
        # verified (GHO-70: Trey 272 and Margie 1635 were submitted after one).
        self.resume_upload_failed = ""
        # The external submit gate saw this job's file on the form itself /
        # let a submit through because the form showed no resume field.
        self.resume_seen_on_page = False
        self.no_resume_field_at_submit = False
        # Field key -> the options our last FIELD_SET_FAILED offered for it.
        self.offered_options: dict[str, list[str]] = {}

    def mark_verified(self) -> None:
        self.easy_apply_verified = True
        self.easy_apply_seen = True
        self._write()

    def resume_upload_done(self) -> None:
        self.resume_uploads_ok += 1
        self._write()

    def resume_upload_failure(self, detail: str) -> None:
        self.resume_upload_failed = (detail or "upload failed")[:200]
        self._write()

    def clear_page_evidence(self) -> None:
        """Drop an earlier attempt's veto record (the folder is per job)."""
        from module4 import easy_apply

        path = easy_apply.guard.seen_file(self.state_file)
        if path:
            Path(path).unlink(missing_ok=True)

    def absorb_page_evidence(self) -> None:
        """The plugin's veto let a direct submit click through because the page
        showed this job's own file (GHO-78: a resumed Workday draft, no new
        upload, no NEED_CLICK)."""
        from module4 import easy_apply

        if self.resume_seen_on_page or not self.state_file:
            return
        seen = easy_apply.guard.read_seen(self.state_file, self.upload_name)
        if seen:
            self.resume_seen_on_page = True
            logger.info("[%s] Resume check passed: the page showed this job's own file %s at a direct %s",
                        self.job_id, self.upload_name, seen.get("what") or "submit")

    def guard_state(self) -> dict:
        return {"job_id": self.job_id, "upload_name": self.upload_name,
                "verified": self.easy_apply_verified,
                "resume_uploads_ok": self.resume_uploads_ok,
                "resume_upload_failed": "" if self.resume_uploads_ok else self.resume_upload_failed}

    def _write(self) -> None:
        if not self.state_file:
            return
        try:
            Path(self.state_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self.state_file).write_text(json.dumps(self.guard_state()))
        except OSError as e:  # the driver-side gate still holds the state
            logger.warning("[%s] Could not write the resume guard state: %s", self.job_id, e)

    def env(self) -> dict[str, str]:
        if not self.upload_name:
            return {}
        return {"JOBO_UPLOAD_RESUME_NAME": self.upload_name,
                "JOBO_RESUME_GUARD_FILE": self.state_file}


_JOB = _JobContext()


PINNED_HERMES = Path(__file__).resolve().parent.parent / "scripts" / "hermes_pinned.sh"


def _pin_dir() -> Path:
    raw = os.environ.get("JOBO_HERMES_PIN_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".hermes" / "jobo-hermes-pin" / "current"


def _hermes_bin() -> str:
    """The `hermes` the apply agent runs: JOBO_HERMES_BIN, else the pinned tree
    (scripts/hermes_pinned.sh, GHO-13) when it exists, else `hermes` on PATH."""
    explicit = os.environ.get("JOBO_HERMES_BIN", "").strip()
    if explicit:
        return explicit
    if (_pin_dir() / "hermes_cli" / "main.py").is_file():
        return str(PINNED_HERMES)
    path = shutil.which("hermes")
    if not path:
        raise RuntimeError("hermes CLI not found on PATH.")
    return path


def hermes_source_root(binary: str) -> Path | None:
    """The Hermes source tree a launcher runs, or None if it cannot be told."""
    if Path(binary).resolve() == PINNED_HERMES.resolve():
        return _pin_dir().resolve()
    try:
        text = Path(binary).read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return None
    # The installer's sh shim execs the real launcher, which puts the tree on sys.path.
    m = re.search(r"^exec\s+(/\S+/bin/hermes)\s", text, re.MULTILINE)
    if m and Path(m.group(1)).resolve() != Path(binary).resolve():
        return hermes_source_root(m.group(1))
    m = re.search(r"sys\.path\.insert\(0,\s*['\"]+([^'\"]+)['\"]+\)", text)
    return Path(m.group(1)) if m else None


def hermes_gateway_only(binary: str) -> bool:
    """True when this Hermes runs every chat turn in its shared gateway process.

    Hermes' unified gateway runtime (auto-updated in on 15 Oct 2026) does that,
    and the gateway never sees the env the apply agent is started with: not
    BROWSER_CDP_URL, so every agent drove a fresh local headless browser
    instead of the worker's CDP browser, and not the JOBO_* values the
    jobo-browser plugin reads. Nothing could submit, while jobs were still
    charged attempts (GHO-13). Such a tree swaps GatewayAgent in for AIAgent.
    A layout this cannot read counts as fine, so it never blocks a working
    install.
    """
    root = hermes_source_root(binary)
    if root is None:
        return False
    setup = root / "hermes_cli" / "cli_agent_setup_mixin.py"
    try:
        return "GatewayAgent as AIAgent" in setup.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def hermes_profile() -> str | None:
    """The Hermes profile the apply agent runs in (see scripts/setup_hermes_profile.sh).

    None means the default profile: used when JOBO_HERMES_PROFILE=default, or
    when the profile has not been created yet (with a warning, so a missing
    setup step degrades the run instead of stopping it).
    """
    name = os.environ.get("JOBO_HERMES_PROFILE", "jobo").strip()
    if not name or name == "default":
        return None
    if not (Path.home() / ".hermes" / "profiles" / name).is_dir():
        logger.warning(
            "Hermes profile %r not found; using the default profile. "
            "Run scripts/setup_hermes_profile.sh to create it.",
            name,
        )
        return None
    return name


def _state_db(profile: str | None) -> Path:
    home = Path.home() / ".hermes"
    return (home / "profiles" / profile if profile else home) / "state.db"


def session_api_calls(session_id: str, profile: str | None) -> int | None:
    """Model calls Hermes has recorded for a session, or None if unreadable."""
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{_state_db(profile)}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT api_call_count FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            con.close()
        return int(row[0]) if row and row[0] is not None else None
    except Exception as e:  # pragma: no cover - accounting must never break a run
        logger.debug("Could not read api_call_count for %s: %s", session_id, e)
        return None


def step_budget(*, is_retry: bool, budget_boost: bool) -> int:
    """Total model calls allowed for one application attempt."""
    raw = os.environ.get("JOBO_APPLY_MAX_TURNS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if budget_boost:
        return AGENT_STEPS_ESCALATED
    return AGENT_STEPS_RETRY if is_retry else AGENT_STEPS_FIRST


def _env_for_agent(cdp_url: str, source_url: str = "") -> dict[str, str]:
    env = dict(os.environ)
    # The job's listing URL, for the jobo-browser plugin's tab keeper: once the
    # agent is on the employer's site, the listing tab is closed so every
    # Hermes browser view agrees on one page (hermes_plugin/jobo-browser/tab_keeper.py).
    if source_url:
        env["JOBO_SOURCE_URL"] = source_url
    # Point Hermes's native browser tools at our persistent, signed-in
    # Chromium instance - the same override mechanism `/browser connect`
    # uses interactively (see hermes tools/browser_tool.py precedence chain).
    env["BROWSER_CDP_URL"] = cdp_url
    # This job's upload file name + guard state, for the plugin's resume_guard.
    env.update(_JOB.env())
    return env


def parse_verdict(text: str) -> ApplyOutcome:
    status, detail = "stuck", ""
    # Use the LAST occurrence of each marker: with the OTP handoff the text
    # may be several concatenated agent rounds, and the final round holds the
    # real verdict. First-match would return a stale intermediate line.
    matches = list(re.finditer(r"^STATUS:\s*(\S+)", text, re.MULTILINE))
    saw_status_marker = bool(matches)
    if matches:
        status = matches[-1].group(1).strip().lower()
    matches = list(re.finditer(r"^DETAIL:\s*(.+)$", text, re.MULTILINE))
    if matches:
        # Keep the WHOLE detail. This truncation is upstream of everything that
        # reads it, so shortening here silently breaks the consumers rather
        # than merely shortening a display string.
        #
        # job516 (2026-09-11) is the worked example: the agent wrote "...but I
        # ran out of tool-call budget before ... clicking the Submit button",
        # the 300-char cut removed the phrase, `runner._is_budget_exhaustion()`
        # returned False, and the retry got the SHORT budget instead of the
        # escalated one - for a job that had filled the entire form and needed
        # only more room. `persist.record_failed_attempt` was widened to 600
        # chars for exactly this reason, but that fix was useless while the
        # string arrived here already cut to 300.
        #
        # The generic rule, now bitten twice: when a persisted field is BOTH
        # displayed and READ BY A DECISION FUNCTION, never truncate it on the
        # way in. Truncate at the point of display only.
        detail = matches[-1].group(1).strip()
    if status == "captcha_wall" and not captcha_walls.gate_host_in_text(detail):
        # Parking is only for the gate hosts in captcha_walls; anywhere else it
        # is the captcha_failed it always was (and must not wall that host).
        status = "captcha_failed"
    matches = list(EMAIL_APPLY_RE.finditer(text))
    email_mailto = matches[-1].group(1) if matches else ""
    if status == "stuck" and email_apply.no_web_form(detail):
        # Agents trained on the old prompt still call a mailto-only posting
        # stuck (Proseware 1684, Lamna 1844); the address is in the detail.
        email_mailto = email_mailto or detail
        status = STATUS_EMAIL_APPLY if email_apply.parse_mailto(email_mailto) else status
    if status == STATUS_EMAIL_APPLY and not email_apply.parse_mailto(email_mailto or detail):
        status = "stuck"
        detail = f"email_apply without a mailto address: {detail}"
    if status not in VALID_STATUSES:
        status = "stuck"
        detail = detail or f"unparseable agent verdict: {text[-200:]}"
    elif not saw_status_marker and not detail:
        # The agent printed NO verdict at all - it died before reaching the
        # end (model provider unreachable, process killed, empty output).
        # `status` is still the "stuck" default and `detail` is empty, which
        # used to persist as a bare `stuck:` with no reason at all.
        #
        # This is exactly the diagnostic black hole the debug-transcript dump
        # was added to fix, except the DB row and the audit receipt still said
        # nothing. On the 2026-09-15 run a ~6-minute model-provider outage
        # produced 18 of these; every one looked like an ordinary site failure
        # and each charged the job a real attempt.
        #
        # Keep the tail of whatever the process did emit: it is what tells a
        # later reader (and `_is_infra_failure`) that this was our fault.
        tail = (text or "").strip()[-400:]
        detail = (
            f"agent produced no verdict; last output: {tail}"
            if tail
            else "agent produced no output and no verdict"
        )

    fields_filled: dict = {}
    matches = list(re.finditer(r"^FIELDS_FILLED_JSON:\s*(\{.*\})\s*$", text, re.MULTILINE))
    if matches:
        try:
            parsed = json.loads(matches[-1].group(1))
            if isinstance(parsed, dict):
                fields_filled = parsed
        except json.JSONDecodeError:
            logger.warning("FIELDS_FILLED_JSON present but not valid JSON, dropping.")

    return ApplyOutcome(status=status, detail=submission_audit.scrub_passwords(detail),
                        fields_filled=fields_filled, email_mailto=email_mailto)


def _dump_debug_transcript(job_id: int, raw_output: str) -> None:
    """Persist the FULL raw agent output on any non-submitted outcome.

    parse_verdict only keeps a ~300-char DETAIL string in the DB, which has
    repeatedly been too little to diagnose "stuck:" with an empty or vague
    reason (e.g. the agent got hard-killed by max-turns before printing a
    STATUS line at all). This keeps the full transcript around so a later
    debugging pass doesn't hit a wall.
    """
    try:
        DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = DEBUG_LOG_DIR / f"job{job_id}_{stamp}.txt"
        path.write_text(raw_output)
    except Exception as e:  # pragma: no cover - best-effort diagnostics only
        logger.debug("Failed to dump debug transcript for job %s: %s", job_id, e)


def _run_hermes(
    *,
    prompt: str,
    cdp_url: str,
    timeout_sec: int,
    max_turns: str,
    resume_session: str | None = None,
    source_url: str = "",
) -> tuple[str, bool]:
    """One `hermes chat` invocation. Returns (combined_output, timed_out).

    resume_session continues an existing session (used for the OTP handoff)
    so the agent keeps its full page context and reattaches to the same
    persistent browser instead of starting the form over.
    """
    binary = _hermes_bin()
    profile = hermes_profile()
    cmd = [binary, *(["-p", profile] if profile else []), "chat", "--query-file", "-", "-Q"]
    if resume_session:
        cmd += ["--resume", resume_session]
    cmd += [
        "--reasoning",
        os.environ.get("JOBO_APPLY_REASONING", "").strip() or "medium",
        "--no-restore-cwd",
        # No SOUL.md persona, no memory, no AGENTS.md: until 2026-09-28 every
        # apply step carried the user's personal notes (~7k chars) for no benefit.
        "--ignore-rules",
        "--max-turns",
        max_turns,
        "-t",
        APPLY_AGENT_TOOLSETS,
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=_env_for_agent(cdp_url, source_url),
        )
        return (proc.stdout or "") + "\n" + (proc.stderr or ""), False
    except subprocess.TimeoutExpired as e:

        def _to_text(x) -> str:
            if isinstance(x, bytes):
                return x.decode("utf-8", errors="replace")
            return x or ""

        out = _to_text(e.stdout) + "\n" + _to_text(e.stderr)
        out += f"\n[jobo] agent subprocess hard-killed after {timeout_sec}s timeout."
        return out, True


def _open_page_urls(cdp_url: str) -> tuple[str, ...]:
    """URLs of the browser's open tabs, best effort (read-only /json/list).

    Used only to read an ATS tenant name (treyco.wd1.myworkdayjobs.com ->
    "treyco") for the OTP employer check; any failure just returns ().
    """
    if not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?/?$", cdp_url or ""):
        # Only the local CDP endpoint; never resolve or contact anything else.
        return ()
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=3) as r:
            tabs = json.loads(r.read().decode())
    except Exception:
        return ()
    return tuple(
        str(t.get("url") or "") for t in tabs if isinstance(t, dict) and t.get("type") == "page"
    )


_WORKDAY_HOST = re.compile(r"(?:^|\.)myworkday(?:jobs|site)\.com$", re.I)


def _seen_workday(cdp_url: str) -> bool:
    """True once a handoff helper has seen this worker's browser on a Workday
    tenant (browser_session.note_tabs; no browser request of its own)."""
    from module4 import browser_session

    return any(_WORKDAY_HOST.search(h) for h in browser_session.seen_hosts(cdp_url))



# --- Attach canary (GHO-17) -------------------------------------------------
# After the 15 Oct 2026 07:00 Hermes auto-update, every apply agent drove its
# own scratch headless Chromium instead of the worker's CDP browser. The
# worker's browser sat on chrome://newtab while agents "saw" LinkedIn signed
# out and uploads found no file input; the run burned 19 min and 31 attempts
# before a human stopped it. One tiny agent turn before the first claim
# proves the agent's browser tools land on THIS worker's browser.
# A public page: Hermes' URL policy refuses about:blank as "a private or
# internal address". example.com is IANA's reserved example domain; the
# fragment survives into /json/list.
CANARY_MARKER_PREFIX = "https://example.com/#jobo-canary-"
CANARY_TIMEOUT_SEC = 180
CANARY_TRIES = 2


@dataclass
class AttachCheck:
    ok: bool
    detail: str


def canary_enabled() -> bool:
    """JOBO_ATTACH_CANARY=0 turns the check off (the offline regression
    harness, which refuses agent subprocesses, sets it)."""
    return os.environ.get("JOBO_ATTACH_CANARY", "1").strip().lower() not in ("0", "false", "no", "off")


def attach_canary(cdp_url: str, worker: str = "main", *, run_hermes=None, page_urls=None,
                  tries: int = CANARY_TRIES) -> AttachCheck:
    """Have the agent open a jobo-owned marker URL, then look for it in this
    browser's own /json/list. Found = attached. Anything else (another
    browser got it, the agent never navigated, the model was unreachable)
    is a failed check: no apply agent on this worker could work either.
    A second try absorbs one flaky turn before a whole run is refused.
    """
    run_hermes = run_hermes or _run_hermes
    page_urls = page_urls or _open_page_urls
    tag = re.sub(r"[^A-Za-z0-9_-]", "", worker) or "main"
    detail = ""
    for attempt in range(1, tries + 1):
        marker = f"{CANARY_MARKER_PREFIX}{tag}-{secrets.token_hex(4)}"
        prompt = (
            "This is a browser connectivity check, not a job application. Call the "
            f"browser_navigate tool exactly once with the url {marker} and call no other "
            "tool. Then reply with the single line:\nCANARY: done"
        )
        out, timed_out = run_hermes(
            prompt=prompt, cdp_url=cdp_url, timeout_sec=CANARY_TIMEOUT_SEC, max_turns="4"
        )
        urls = page_urls(cdp_url)
        if any(marker in u for u in urls):
            return AttachCheck(True, f"agent drives {cdp_url} (marker {marker} found there)")
        tail = " ".join((out or "").split())[-200:]
        detail = (
            f"the agent's browser_navigate to {marker} never reached {cdp_url} "
            f"(its tabs: {', '.join(u[:60] for u in urls[:3]) or 'none'}); "
            + ("the agent timed out" if timed_out else f"agent output tail: {tail}")
        )
        logger.warning("Attach canary try %d/%d failed: %s", attempt, tries, detail)
    return AttachCheck(False, detail)


def _resolve_otp(
    sender: str,
    hint: str,
    job_id: int,
    not_before: datetime | None = None,
    seen: dict | None = None,
    company: str = "",
    employer_urls: tuple[str, ...] = (),
    cdp_url: str = "",
) -> tuple[str | None, str]:
    """Look up a verification code on the agent's behalf.

    Returns (code, human_readable_note). Import is local so a missing Gmail
    token can never break a normal apply run that never needs an OTP.

    not_before: ignore mail received before this instant (see run_apply_agent).
    seen: if given, receives "received_at" (datetime) of the mail used.
    company / employer_urls: the job's employer (DB company_name) and its
    application URLs. Lets mail sent by a white-label ATS in the employer's
    name count as the requested site (job 1429, see otp_lookup.find_code).
    cdp_url: when given with a company, the open tabs' URLs are read once
    (read-only /json/list) for an ATS tenant name such as
    treyco.wd1.myworkdayjobs.com. Done here, after the refusal checks, so only
    a real lookup touches the browser.

    A note prefixed "BLOCKER: " means no code will ever arrive (the portal
    said so) and the caller must stop retrying.
    """
    from module4 import linkedin_session

    if linkedin_session.is_protected_account_domain(sender) or linkedin_session.is_protected_account_domain(hint):
        # Job 1007, 2026-10-03: the agent was signing in to LinkedIn itself and
        # this lookup handed it LinkedIn's security PIN. Never again.
        logger.warning(
            "[%s] REFUSED an emailed-code lookup for a LinkedIn/Google sign-in "
            "(sender=%r, hint=%r).",
            job_id,
            sender,
            hint,
        )
        return None, f"REFUSED: {_ACCOUNT_SIGNIN_REFUSAL}"

    try:
        from module4 import otp_lookup
    except Exception as e:  # pragma: no cover - import guard
        return None, f"OTP lookup unavailable: {e}"

    if company and cdp_url:
        employer_urls = tuple(dict.fromkeys((*employer_urls, *_open_page_urls(cdp_url))))

    logger.info(
        "[%s] Agent requested an emailed code (sender filter=%r, hint=%r). Polling inbox...",
        job_id,
        sender,
        hint,
    )
    # In a parallel run (GHO-5) other workers read the same inbox. One lookup
    # at a time across workers, mail another job already used is skipped,
    # and a code from an ATS family another worker is on right now is not
    # handed over: it could be that worker's code.
    from module4 import apply_queue

    run_queue = _parallel_queue()
    try:
        with _otp_lock(run_queue):
            result = otp_lookup.wait_for_code(
                sender=sender,
                minutes=OTP_WINDOW_MINUTES,
                subject_hint=hint or "",
                timeout_sec=OTP_WAIT_SEC,
                not_before=not_before,
                company=company,
                employer_urls=employer_urls,
                skip_ids=(lambda: run_queue.otp_consumed_keys(exclude_job=job_id)) if run_queue else None,
            )
            if run_queue and (result.found or result.magic_link):
                family = apply_queue.family_for_sender(result.sender)
                if family and family in run_queue.other_inflight_families(_worker_name()):
                    logger.warning(
                        "[%s] Not handing over the code from %s: another worker is on %s right now.",
                        job_id,
                        result.sender,
                        family,
                    )
                    return None, (
                        f"the only matching mail came from {family}, a site another application "
                        "in this run is using right now, so it may not be this job's code. Wait "
                        "about a minute, click the page's resend-code link"
                    )
                if not run_queue.consume_otp(result.message_id, job_id):
                    return None, "that mail was already used by another job"
    except Exception as e:
        return None, f"OTP lookup raised: {e}"

    if (result.found or result.magic_link) and linkedin_session.is_protected_account_domain(
        result.sender
    ):
        # The unfiltered fallback search can still land on LinkedIn/Google
        # security mail (1007's code came that way). Never hand it over.
        # Checked before `seen` is updated: a refused mail was never used.
        logger.warning(
            "[%s] REFUSED to pass on a code from %s: LinkedIn/Google sign-in mail.",
            job_id,
            result.sender,
        )
        return None, f"REFUSED: {_ACCOUNT_SIGNIN_REFUSAL}"
    if seen is not None and result.received_at:
        try:
            seen["received_at"] = datetime.fromisoformat(result.received_at)
        except ValueError:
            pass
    if result.found:
        logger.info("[%s] Retrieved emailed code from %s.", job_id, result.sender or sender)
        return result.code, f"code retrieved from {result.sender or sender}"
    if result.magic_link:
        # No code exists - this portal mails a one-click sign-in URL. Hand the
        # agent the URL and tell it to navigate, rather than reporting "no
        # email arrived" and burning the remaining OTP rounds (job651,
        # 2026-09-05: Goodfit sent two auth links, both ignored).
        logger.info(
            "[%s] Retrieved sign-in LINK (no code) from %s.", job_id, result.sender or sender
        )
        return None, (
            "NO CODE - this portal sent a one-click sign-in or account-activation LINK instead. "
            "Navigate to this URL to authenticate (after an activation link, sign in with the "
            f"password you set if asked), then continue the application: {result.magic_link}"
        )
    if result.blocker:
        logger.warning(
            "[%s] No code will arrive: %s (subject=%r)",
            job_id,
            result.blocker[:80],
            result.subject[:50],
        )
        return None, f"BLOCKER: {result.blocker}"
    logger.warning("[%s] No emailed code found: %s", job_id, result.error)
    return None, result.error or "no code found"


def _parallel_queue():
    """The run's shared queue when this process is a parallel-run worker."""
    from module4 import apply_queue

    run_id = apply_queue.current_run_id()
    return apply_queue.ApplyQueue(run_id) if run_id else None


def _worker_name() -> str:
    return os.environ.get("JOBO_APPLY_WORKER", "").strip()


class _otp_lock:
    """Cross-worker lock around one emailed-code lookup (no-op outside a parallel run)."""

    def __init__(self, run_queue):
        self.path = run_queue.path.with_name("otp_handoff.lock") if run_queue else None
        self.fh = None

    def __enter__(self):
        if self.path is not None:
            import fcntl

            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            self.fh.close()  # closing releases the flock
        return False


def _resolve_upload(
    line: str,
    *,
    cdp_url: str,
    resume_path: str,
    cover_path: str | None,
    job_id: int,
) -> str:
    """Perform the upload the agent asked for and return a message for it.

    The agent never touches CDP for this. It names a slot; we do the pierced
    tree walk, set the files, dispatch change events, and verify by reading
    element.files back out of the page.
    """
    parsed = None
    try:
        from module4 import uploader
    except Exception as e:  # pragma: no cover - import guard
        return f"UPLOAD_FAILED: uploader unavailable ({e}). Continue without it."

    parsed = uploader.parse_request(line)
    want, hint = parsed if parsed else ("resume", "")
    path = resume_path if want == "resume" else (cover_path or resume_path)
    if want == "cover" and not cover_path:
        return (
            "UPLOAD_FAILED: no cover letter was generated for this job, so there "
            "is nothing to attach. If the field is optional, skip it and continue; "
            "if it is required, attach the resume there instead by asking for "
            "NEED_UPLOAD: resume | <that field's attribute>."
        )

    logger.info("[%s] Agent requested upload (want=%s, hint=%r).", job_id, want, hint)
    if want == "resume":
        # LinkedIn Easy Apply has no file input until "Upload resume" is
        # pressed, and lists saved resumes as pre-selected cards. Upload there
        # through the modal's own button and select our card (TODO item 18).
        from module4 import easy_apply

        try:
            ea = easy_apply.upload_and_select(cdp_url, path)
        except Exception as e:
            logger.warning("[%s] Easy Apply upload raised: %s", job_id, e)
            ea = easy_apply.EasyApplyUpload(False, f"Easy Apply upload raised: {e}")
        if ea is not None:
            if ea.ok:
                _JOB.resume_uploads_ok += 1
                _JOB.mark_verified()  # writes the guard state too
                logger.info("[%s] Upload SUCCEEDED (LinkedIn Easy Apply): %s", job_id, ea.detail)
            else:
                _JOB.easy_apply_seen = True
                logger.warning("[%s] Easy Apply upload failed: %s", job_id, ea.detail)
            return ea.as_agent_message()
    promoted = _promote_embedded_form(cdp_url, job_id)
    try:
        result = uploader.upload_file(cdp_url, path, want=want, hint=hint)
    except Exception as e:
        logger.warning("[%s] Upload raised: %s", job_id, e)
        return promoted + f"UPLOAD_FAILED: {e}"

    if result.ok:
        if want == "resume":
            _JOB.resume_upload_done()
        logger.info("[%s] Upload SUCCEEDED: %s", job_id, result.detail)
    else:
        if NO_FILE_INPUT in (result.detail or ""):
            # No slot here (yet): not a failed upload of the resume.
            _JOB.upload_no_file_input += 1
        elif want == "resume":
            _JOB.resume_upload_failure(result.detail)
        logger.warning("[%s] Upload failed: %s", job_id, result.detail)
    return promoted + result.as_agent_message()


def _promote_embedded_form(cdp_url: str, job_id: int) -> str:
    """Open an iframe-embedded ATS form in the tab (module4/embedded_forms.py).

    Returns the note for the agent, ending in a blank line, or ''.
    """
    try:
        from module4 import embedded_forms

        note = embedded_forms.promote(cdp_url)
    except Exception as e:  # pragma: no cover - promotion is best-effort
        logger.warning("[%s] embedded form check raised: %s", job_id, e)
        return ""
    if note:
        logger.info("[%s] Opened the embedded application form in the tab.", job_id)
        return note + "\n\n"
    return ""


def _resolve_field(
    line: str, *, cdp_url: str, job_id: int, failures: dict[str, int]
) -> str:
    """Set a form field on the agent's behalf and return a message for it.

    `failures` tracks consecutive failures PER FIELD. Three strikes on the same
    field and we tell the agent to move on: on the 2026-09-03 run the dominant
    failure was an agent burning its entire budget on ONE stubborn control, and
    a 25-round global cap is far too loose to prevent that.
    """
    try:
        from module4 import form_filler
    except Exception as e:  # pragma: no cover - import guard
        return f"FIELD_SET_FAILED: form filler unavailable ({e}). Fill it manually."

    parsed = form_filler.parse_request(line)
    if not parsed:
        return (
            "FIELD_SET_FAILED: could not parse that directive. Use exactly "
            "NEED_FIELD: #<number> = <value>  (get numbers with NEED_FIELDS)"
        )
    field_hint, value, index = parsed
    key = f"#{index}" if index else field_hint.strip().lower()
    value, password_note = _enforce_account_password(
        cdp_url, field_hint, value, index, form_filler=form_filler, job_id=job_id
    )

    # A value picked from the list our own failure reply just offered is the
    # answer we asked for, not a 4th guess (Reputation 1464, GHO-72: told
    # "these are the ONLY values: Corporate Website", then refused that pick).
    offered = _JOB.offered_options.pop(key, [])
    picked_offer = (failures.get(key, 0) <= MAX_FAILURES_PER_FIELD and
                    any(_norm_option(o) == _norm_option(value) for o in offered))
    if failures.get(key, 0) >= MAX_FAILURES_PER_FIELD and not picked_offer:
        logger.warning(
            "[%s] Field %r failed %d times; telling the agent to move on.",
            job_id,
            key,
            MAX_FAILURES_PER_FIELD,
        )
        return (
            f"FIELD_SET_ABANDONED: {field_hint!r} has failed "
            f"{MAX_FAILURES_PER_FIELD} times and will not be retried.\n\n"
            "Stop working on this field. If it is optional, leave it blank. If it "
            "is required and the form will not submit without it, fill everything "
            "else, try Submit once, and if it is genuinely rejected print your "
            "STATUS/DETAIL/FIELDS_FILLED_JSON verdict. Do NOT ask for this field "
            "again."
        )

    logger.info(
        "[%s] Agent requested field set: %s = %r",
        job_id,
        f"#{index}" if index else repr(field_hint),
        value,
    )
    refused = _made_up_value_refusal(cdp_url, field_hint, value, index, form_filler=form_filler)
    if refused:
        failures[key] = failures.get(key, 0) + 1
        logger.warning(
            "[%s] Field set refused for %s: a made-up value not in the candidate facts.",
            job_id,
            key,
        )
        return f"FIELD_SET_REFUSED: {refused}"
    try:
        result = form_filler.set_field(cdp_url, field_hint, value, index=index)
    except Exception as e:
        logger.warning("[%s] Field set raised: %s", job_id, e)
        failures[key] = failures.get(key, 0) + 1
        return f"FIELD_SET_FAILED: {e}"

    if result.ok:
        failures.pop(key, None)
        if result.substituted:
            # Worth its own log line: the application will carry the SITE's
            # wording, not what the agent asked for. Silent substitution is how
            # you find out months later that every form said something slightly
            # different from what you thought.
            logger.info(
                "[%s] Field SET: %s = %r (asked for %r; site had no such option)",
                job_id,
                result.matched or field_hint,
                result.substituted,
                result.value,
            )
        else:
            logger.info("[%s] Field SET: %s", job_id, result.matched or field_hint)
    elif result.stale:
        # Nothing was typed, and after a re-list "#18" names another control,
        # so this is no strike against any field.
        logger.warning("[%s] Field set not run for %s: %s", job_id, key, result.note[:100])
    else:
        failures[key] = failures.get(key, 0) + 1
        if result.no_match and result.available_options:
            _JOB.offered_options[key] = list(result.available_options)
        logger.warning(
            "[%s] Field set failed (%d/%d for %s): %s",
            job_id,
            failures[key],
            MAX_FAILURES_PER_FIELD,
            key,
            result.note[:100],
        )
    message = result.as_agent_message()
    return f"{password_note}\n\n{message}" if password_note else message


def _norm_option(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _made_up_value_refusal(
    cdp_url: str, field_hint: str, value: str, index: int | None, *, form_filler
) -> str:
    """The agent message when `value` is a postal code or street number the
    candidate facts do not give (policy/made_up_values.py), else ""."""
    try:
        listed = form_filler.listed_field(cdp_url, index)
    except Exception:
        listed = None
    label = listed[1] if listed else ("" if index else field_hint)
    facts = personal.candidate_facts() + "\n" + json.dumps(personal.candidate())
    kind = made_up_values.refusal(label, value, facts)
    return made_up_values.agent_message(kind, label, value) if kind else ""


_PASSWORD_LABEL_RE = re.compile(r"pass\s*word|passcode|\bpwd\b", re.I)


def _enforce_account_password(
    cdp_url: str, field_hint: str, value: str, index: int | None, *, form_filler, job_id: int
) -> tuple[str, str]:
    """A password field only ever gets the pipeline's standard password.

    Wingtip job 818 (2026-10-03) typed three different made-up passwords
    (three variations of "<FirstName>@Zeno2026!") into
    an Avature registration form through this handoff. Any account created
    that way is one no later run can sign in to. The intended policy
    (password_reset.py) is ONE known password everywhere, so a
    later run signs in instead of resetting again.

    The exception is a portal listed in secrets.json `site_passwords`: an
    earlier run already set a different password there (Wingtip 929), so that
    one is the password its fields get.
    """
    from module4 import password_reset

    try:
        listed = form_filler.listed_field(cdp_url, index)
    except Exception:
        listed = None
    kind, label = listed if listed else ("", "")
    is_password = kind == "password" or bool(
        _PASSWORD_LABEL_RE.search(f"{label} {field_hint if not index else ''}")
    )
    if not is_password or re.search(r"forgot|reset link|strength", label or field_hint, re.I):
        return value, ""
    site, recorded = _recorded_site_password(cdp_url, form_filler)
    standard = recorded or password_reset.STANDARD_PASSWORD
    if not standard or value == standard:
        return value, ""
    logger.warning(
        "[%s] Password field %s: replacing an agent-chosen password with the "
        "%s.",
        job_id,
        f"#{index}" if index else repr(field_hint),
        f"one recorded for {site}" if recorded else "pipeline's standard one",
    )
    if recorded:
        return standard, (
            f"NOTE: the account on {site} already exists and an earlier run set its "
            f"password to {standard}, so this field was set to {standard} instead of "
            "the value you gave. Sign in with it; do not reset it or try others."
        )
    return standard, (
        f"NOTE: account passwords are never invented. This field was set to the "
        f"pipeline's standard password {standard} instead of the value you gave. "
        f"Use exactly {standard} for the confirm-password field and for any later "
        "sign-in on this portal. If the portal rejects it, report STATUS: stuck "
        "quoting its password rule; do not try other passwords."
    )


def _recorded_site_password(cdp_url: str, form_filler) -> tuple[str, str]:
    """(host, password) when an open tab is on a `site_passwords` portal."""
    if not personal.site_passwords():
        return "", ""
    try:
        tabs = form_filler._page_targets(cdp_url)
    except Exception:
        return "", ""
    for tab in tabs:
        host = (urllib.parse.urlsplit(str(tab.get("url") or "")).hostname or "").lower()
        recorded = personal.site_password(host)
        if recorded:
            return host, recorded
    return "", ""


def _easy_apply_submit_refusal(label: str, *, cdp_url: str, job_id: int) -> str:
    """Refusal message when this click could send an Easy Apply application
    without THIS job's resume selected, or any other form without it verified
    (_external_submit_refusal), else ''.

    Only submit-like requests are checked (a blank NEED_CLICK means "the
    submit button"). Consent checkboxes and navigation pass straight through.
    """
    if not _JOB.upload_name or not _SUBMITISH_CLICK_RE.search(label or ""):
        return ""
    from module4 import easy_apply

    try:
        allowed, reason, seen = easy_apply.submit_gate(
            cdp_url, _JOB.upload_name, _JOB.easy_apply_verified)
    except Exception as e:  # cannot verify: never risk an unverifiable send
        allowed, reason, seen = False, f"could not verify the selected resume ({e})", True
    if seen:
        _JOB.easy_apply_seen = True
    if allowed:
        if seen:
            # The modal itself shows this job's file selected (e.g. LinkedIn
            # kept the card from an earlier attempt: same name = same bytes).
            _JOB.mark_verified()
            logger.info("[%s] Easy Apply resume check passed: %s", job_id, reason)
            return ""
        return _external_submit_refusal(cdp_url=cdp_url, job_id=job_id)
    logger.warning("[%s] REFUSED Easy Apply submit: %s", job_id, reason)
    return "CLICK_FAILED: " + easy_apply.guard.refusal_message(reason, _JOB.upload_name).replace(
        "BLOCKED by jobo: ", "", 1) + "\nNo click was sent."


def _external_submit_refusal(*, cdp_url: str, job_id: int) -> str:
    """Refusal message when a submit-like click on any other site would send
    the application without THIS job's resume verified, else '' (GHO-70)."""
    from module4 import easy_apply

    try:
        allowed, reason, shown = easy_apply.external_submit_gate(
            cdp_url, _JOB.upload_name, _JOB.guard_state())
    except Exception as e:  # pragma: no cover - external_submit_gate catches page errors
        allowed, reason, shown = not _JOB.resume_upload_failed, f"resume check raised ({e})", False
    if shown and not _JOB.resume_uploads_ok:
        # The unique per-job name on the form is proof our upload landed.
        _JOB.resume_seen_on_page = True
        logger.info("[%s] Resume check passed: %s", job_id, reason)
    if allowed:
        if reason == "this form has no resume field":
            _JOB.no_resume_field_at_submit = True
            logger.info("[%s] Submit allowed without a resume upload: %s", job_id, reason)
        return ""
    logger.warning("[%s] REFUSED submit (resume not verified): %s", job_id, reason)
    return "CLICK_FAILED: " + easy_apply.guard.refusal_message_external(reason, _JOB.upload_name).replace(
        "BLOCKED by jobo: ", "", 1) + "\nNo click was sent."


def _dismiss_refusal(label: str, *, cdp_url: str, job_id: int) -> str:
    """Refusal message when this click would close a resume upload dialog
    after this job's resume upload failed, else '' (GHO-100: Duruper's
    "Cancel" on its CV dialog sent Wren 2909 with no CV)."""
    from module4 import easy_apply

    if not _JOB.upload_name or not easy_apply.guard.DISMISS_NAME.search(label or ""):
        return ""
    allowed, reason = easy_apply.external_dismiss_gate(cdp_url, _JOB.upload_name, _JOB.guard_state())
    if allowed:
        return ""
    logger.warning("[%s] REFUSED dismiss (resume upload failed): %s", job_id, reason)
    return "CLICK_FAILED: " + easy_apply.guard.refusal_message_dismiss(reason, _JOB.upload_name).replace(
        "BLOCKED by jobo: ", "", 1) + "\nNo click was sent."


def _resolve_click(line: str, *, cdp_url: str, job_id: int) -> str:
    """Click a button for the agent and report what actually changed."""
    try:
        from module4 import clicker
    except Exception as e:  # pragma: no cover - import guard
        return f"CLICK_FAILED: clicker unavailable ({e}). Click it yourself."

    label = clicker.parse_request(line)
    if label is None:
        return "CLICK_FAILED: could not parse that directive. Use NEED_CLICK: <label>"

    logger.info("[%s] Agent requested a click: %r", job_id, label or "(submit)")
    refusal = (_dismiss_refusal(label, cdp_url=cdp_url, job_id=job_id)
               or _easy_apply_submit_refusal(label, cdp_url=cdp_url, job_id=job_id))
    if refusal:
        return refusal
    try:
        result = clicker.click(cdp_url, label)
    except Exception as e:
        logger.warning("[%s] Click raised: %s", job_id, e)
        return f"CLICK_FAILED: {e}"

    if result.ok:
        logger.info("[%s] Click SUCCEEDED: %s", job_id, result.changed[:120])
    else:
        logger.warning("[%s] Click failed: %s", job_id, result.note[:120])
    return result.as_agent_message()


def _resolve_slider(*, cdp_url: str, job_id: int) -> str:
    """Solve the page's jigsaw slider captcha for the agent."""
    try:
        from module4 import slider_captcha
    except Exception as e:  # pragma: no cover - import guard
        return f"SLIDER_FAILED: slider solver unavailable ({e})."
    logger.info("[%s] Agent requested the slider captcha solver.", job_id)
    try:
        result = slider_captcha.solve(cdp_url)
    except Exception as e:
        logger.warning("[%s] Slider solver raised: %s", job_id, e)
        return f"SLIDER_FAILED: {e}"
    if result.ok:
        logger.info("[%s] Slider SOLVED: %s", job_id, result.note[:160])
    else:
        logger.warning("[%s] Slider failed: %s", job_id, result.note[:160])
    return result.as_agent_message()


def _resolve_password_reset(line: str, *, job_id: int) -> str:
    """Fetch a password-reset link from Gmail and tell the agent what to set.

    The agent never sees the mailbox - it names a domain, the driver returns
    exactly one URL plus the standard password. Same approval-gate shape as
    the OTP handoff.
    """
    try:
        from module4 import password_reset
    except Exception as e:  # pragma: no cover - import guard
        return f"PASSWORD_RESET_FAILED: reset lookup unavailable ({e})."

    domain = password_reset.parse_request(line)
    if not domain:
        return (
            "PASSWORD_RESET_FAILED: could not parse that directive. Use exactly "
            "NEED_PASSWORD_RESET: <domain>"
        )

    from module4 import linkedin_session

    if linkedin_session.is_protected_account_domain(domain):
        logger.warning(
            "[%s] REFUSED a password reset for %s (LinkedIn/Google account).", job_id, domain
        )
        return f"PASSWORD_RESET_REFUSED: {_ACCOUNT_SIGNIN_REFUSAL}"

    otp_site = password_reset.otp_signin_domain(domain)
    if otp_site:
        logger.warning(
            "[%s] REFUSED a password reset for %s (signs in by emailed OTP).", job_id, domain
        )
        return (
            f"PASSWORD_RESET_REFUSED: the candidate's own {otp_site} account is never "
            "password-reset. Sign in with an emailed one-time code instead: on the "
            "sign-in page choose the 'Login with OTP' / email OTP option (not 'Forgot "
            f"password'), enter {personal.email()}, request the code, then end your turn "
            f"with exactly `NEED_OTP: {otp_site}`. Do not open any reset link."
        )

    logger.info("[%s] Agent requested a password reset for %s.", job_id, domain)
    recorded = personal.site_password(domain)
    if recorded:
        # Wingtip 1481 (2026-10-23): five runs reset this account to the
        # standard password, which the portal refuses as one of the last 3,
        # while the password set by Wingtip 929 on 2026-09-18 still worked.
        logger.info("[%s] %s has a recorded account password; no reset.", job_id, domain)
        return (
            f"PASSWORD_RESET_NOT_NEEDED: the account on {domain} for {personal.email()} "
            f"was set up by an earlier jobo run with the password {recorded}. Do not "
            "open any reset link and do not request another reset. Go to the portal's "
            f"normal sign-in page, sign in with {personal.email()} / {recorded}, and "
            "continue the application. If that sign-in is refused, report STATUS: stuck "
            "quoting the portal's message."
        )
    # The mail needs a moment to land; the agent has just clicked "forgot
    # password" in the same turn it printed the directive.
    time.sleep(PASSWORD_RESET_WAIT_SEC)
    try:
        result = password_reset.find_reset_link(domain=domain)
    except Exception as e:
        logger.warning("[%s] Password-reset lookup raised: %s", job_id, e)
        return f"PASSWORD_RESET_FAILED: {e}"

    if not result.ok:
        logger.warning("[%s] No reset link found: %s", job_id, result.error)
        # 1622 Kestrel (Avature, 2026-10-15): two resets, no mail in any
        # folder, because there was no account to reset. The application form
        # itself had Password / Confirm password fields that create one.
        return (
            f"PASSWORD_RESET_FAILED: {result.error}\n\n"
            "Make sure you actually clicked the portal's 'Forgot password' / "
            "'Reset password' control BEFORE asking. Most portals send NO mail "
            f"when no account exists for {personal.email()}, so the likeliest "
            "reason is that there is no account yet: do not request another "
            "reset; create a new account (or continue as a new candidate) with "
            f"password {password_reset.STANDARD_PASSWORD}. Report STATUS: stuck "
            "only if the portal says an account already exists AND no reset "
            "mail arrives."
        )

    logger.info(
        "[%s] Password reset link retrieved (subject=%r).", job_id, result.subject[:60]
    )
    return (
        f"PASSWORD_RESET_LINK: {result.url}\n"
        f"PASSWORD_TO_SET: {result.password}\n\n"
        f"That link was just emailed by {result.sender or domain}. Navigate to it, "
        f"set the password to EXACTLY {result.password} (in both the password and "
        "confirm-password fields), then sign in with "
        f"{personal.email()} / {result.password} and continue the application. "
        "This is now the account's real password, so do not request another reset "
        "for this domain. If the reset page refuses it as a password used before, "
        "the account already has it: leave the reset page and sign in with "
        f"{personal.email()} / {result.password} on the normal sign-in page."
    )


def _listed_total(listing: str) -> int:
    """Controls on the page, from a listing reply (0 if it is not one)."""
    m = _PAGED_TOTAL_RE.search(listing or "") or _LISTED_COUNT_RE.search(listing or "")
    return int(m.group(1)) if m else 0


def _resolve_fields_listing(*, cdp_url: str, job_id: int, start: int = 1) -> str:
    """Enumerate the page's controls and number them for the agent."""
    try:
        from module4 import form_filler
    except Exception as e:  # pragma: no cover - import guard
        return f"FIELDS_LISTED_FAILED: form filler unavailable ({e})."

    logger.info("[%s] Agent requested the numbered field list%s.", job_id,
                f" from #{start}" if start > 1 else "")
    promoted = _promote_embedded_form(cdp_url, job_id)
    try:
        result = form_filler.list_fields(cdp_url, start=start)
    except Exception as e:
        logger.warning("[%s] list_fields raised: %s", job_id, e)
        return promoted + f"FIELDS_LISTED_FAILED: {e}"
    if result.ok:
        form_filler.remember_listing(cdp_url, result)
        logger.info("[%s] Listed %d field(s) for the agent (%d on the page).",
                    job_id, len(result.fields), result.total)
    else:
        logger.warning("[%s] Field listing failed: %s", job_id, result.note[:100])
    return promoted + result.as_agent_message()


def run_apply_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    cdp_url: str,
    job_id: int,
    is_retry: bool = False,
    resume_path: str = "",
    cover_path: str | None = None,
    budget_boost: bool = False,
    source_url: str = "",
    company: str = "",
) -> ApplyOutcome:
    """Launch `hermes chat` with native browser tools; return parsed verdict.

    Handles two mid-run handoffs, both of which pause the agent and resume the
    SAME session so the browser keeps its page state:
      * `NEED_OTP: <sender>`   - driver fetches an emailed verification code
      * `NEED_UPLOAD: <slot>`  - driver attaches a file over CDP deterministically

    budget_boost: give this attempt the FIRST-attempt budget even though it is
    a retry. Used when the previous failure was budget/time exhaustion, where
    cutting the budget (the old behaviour) guaranteed the same failure again.

    company: the job's employer name, handed to the OTP lookup so codes that a
    white-label ATS (Oracle Fusion, Workday, iCIMS, ...) mails in the
    employer's name are accepted (job 1429). Empty keeps the old behaviour.
    """
    global _JOB
    # Upload a byte-identical copy named for this job, so its LinkedIn Easy
    # Apply card (and any ATS file chip) is recognisably ours (TODO item 18).
    from module4 import upload_names

    resume_path = upload_names.prepare(job_id, resume_path, "resume") or resume_path
    cover_path = upload_names.prepare(job_id, cover_path, "cover") if cover_path else cover_path
    upload_name = Path(resume_path).name if resume_path and Path(resume_path).is_file() else ""
    state_file = str(upload_names.copies_dir() / str(int(job_id)) / "resume_guard.json") if upload_name else ""
    _JOB = _JobContext(job_id, upload_name, state_file)
    _JOB._write()
    _JOB.clear_page_evidence()
    try:
        outcome = _run_apply_agent(
            system_prompt=system_prompt, user_prompt=user_prompt, cdp_url=cdp_url,
            job_id=job_id, is_retry=is_retry, resume_path=resume_path,
            cover_path=cover_path, budget_boost=budget_boost, source_url=source_url,
            company=company,
        )
        return _check_resume_rule(outcome, job_id)
    finally:
        _JOB = _JobContext()


def _check_resume_rule(outcome: ApplyOutcome, job_id: int) -> ApplyOutcome:
    """Record the resume evidence; flag a send that broke the user's resume rule.

    Too late to un-send by now (prevention is the NEED_CLICK gate and the
    plugin veto); this makes any miss loud in the log and the audit record.
    """
    outcome.resume_upload_name = _JOB.upload_name
    outcome.resume_uploads_ok = _JOB.resume_uploads_ok
    outcome.easy_apply_seen = _JOB.easy_apply_seen
    outcome.easy_apply_resume_verified = _JOB.easy_apply_verified
    outcome.upload_no_file_input = _JOB.upload_no_file_input
    if outcome.status != "submitted" or not _JOB.upload_name:
        return outcome
    if _JOB.resume_uploads_ok == 0:
        _JOB.absorb_page_evidence()
    outcome.resume_seen_on_page = _JOB.resume_seen_on_page
    said_easy_apply = "easy apply" in (outcome.detail or "").lower()
    if _JOB.easy_apply_seen and not _JOB.easy_apply_verified:
        outcome.resume_rule_violation = (
            "LinkedIn Easy Apply submitted without this job's resume verified as the selected card")
    elif _JOB.resume_uploads_ok == 0 and not _JOB.resume_seen_on_page:
        # Still flagged when the form showed no resume field at submit (an
        # earlier step may have had one), but worded so a review can tell.
        outcome.resume_rule_violation = (
            "LinkedIn Easy Apply submitted with no upload of this job's resume" if said_easy_apply
            else "submitted with no upload of this job's resume; the form showed no resume field at submit"
            if _JOB.no_resume_field_at_submit and not _JOB.resume_upload_failed
            else "submitted with no verified upload of this job's resume")
    if outcome.resume_rule_violation:
        logger.error("[%s] RESUME RULE VIOLATION: %s (upload name %s).",
                     job_id, outcome.resume_rule_violation, _JOB.upload_name)
    return outcome


def _run_apply_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    cdp_url: str,
    job_id: int,
    is_retry: bool = False,
    resume_path: str = "",
    cover_path: str | None = None,
    budget_boost: bool = False,
    source_url: str = "",
    company: str = "",
) -> ApplyOutcome:
    full = (
        f"{system_prompt.strip()}\n\n# Task\n{user_prompt.strip()}\n\n"
        "Begin now. Work autonomously until submitted or blocked."
    )
    if budget_boost:
        # Previous failure was running out of room. Escalate rather than
        # merely restoring the first-attempt budget: these jobs are typically
        # 90%+ through a long form and just need to reach Submit.
        timeout_sec = AGENT_TIMEOUT_ESCALATED_SEC
        logger.info(
            "[%s] Retry gets an ESCALATED budget (%s steps / %ds) - previous "
            "failure was budget/time exhaustion.",
            job_id,
            step_budget(is_retry=is_retry, budget_boost=True),
            timeout_sec,
        )
    elif is_retry:
        timeout_sec = AGENT_TIMEOUT_RETRY_SEC
    else:
        timeout_sec = AGENT_TIMEOUT_FIRST_SEC
    budget = step_budget(is_retry=is_retry, budget_boost=budget_boost)
    base_budget = budget
    profile = hermes_profile()
    # Model calls per session id. Normally one id for the whole job; kept per
    # id so a session Hermes forks (compression) still adds up correctly.
    calls_by_session: dict[str, int] = {}

    deadline = time.monotonic() + timeout_sec
    transcript_parts: list[str] = []
    session_id: str | None = None
    otp_rounds = 0
    upload_rounds = 0
    already_refunds = 0
    field_rounds = 0
    field_list_rounds = 0
    stale_refunds = 0
    new_field_refunds = 0
    auto_relists = 0
    reset_rounds = 0
    click_rounds = 0
    resume_refusals = 0
    slider_rounds = 0
    field_failures: dict[str, int] = {}
    # Oldest mail an OTP lookup may use. A code requested in this attempt
    # cannot predate the attempt (job 1007 ran right after 1005, the same
    # Humongo site, so a stale code was a real possibility). The slack covers
    # clock skew between this host and Gmail's received timestamp. After a
    # code is handed out, later rounds only accept mail newer than that one,
    # so a rejected code is never handed back again.
    otp_not_before = datetime.now(timezone.utc) - timedelta(seconds=OTP_CLOCK_SLACK_SEC)
    # Newer mail is not enough on its own: job 2069 got "micro1" out of three
    # separate resent mails. A value already handed out is never sent twice.
    otp_codes_given: set[str] = set()
    prompt = full
    from module4 import browser_session

    browser_session.forget_tabs(cdp_url)

    account_extra = 0
    progress_extra = 0
    long_form_extra = 0
    started = time.monotonic()
    # Step count at which each field was first set by the pipeline.
    fields_set_at: dict[str, int] = {}

    def account_setup_note() -> str:
        # The first account handoff grows the budget once and tells the agent.
        nonlocal budget, deadline, account_extra
        if account_extra:
            return ""
        account_extra = ACCOUNT_SETUP_STEPS
        budget += ACCOUNT_SETUP_STEPS
        deadline += ACCOUNT_SETUP_SEC
        logger.info(
            "[%s] Account setup: +%d steps, +%ds (budget now %d).",
            job_id, ACCOUNT_SETUP_STEPS, ACCOUNT_SETUP_SEC, budget,
        )
        return (
            f"\n\n[jobo] Account setup does not count against the form: {ACCOUNT_SETUP_STEPS} "
            f"model steps were added, so this job's step budget is now {budget}."
        )

    def long_form_note(listing: str) -> str:
        # The first listing of a long form grows the budget once and tells the
        # agent to fill it by number. A retry is first lifted to the first-try
        # budget (no lift when JOBO_APPLY_MAX_TURNS pins it).
        nonlocal budget, deadline, long_form_extra
        total = _listed_total(listing)
        if long_form_extra or total < LONG_FORM_CONTROLS:
            return ""
        lift = max(0, step_budget(is_retry=False, budget_boost=False) - base_budget)
        long_form_extra = LONG_FORM_STEPS + lift
        budget += long_form_extra
        deadline = max(deadline, started + AGENT_TIMEOUT_FIRST_SEC) + LONG_FORM_SEC
        logger.info(
            "[%s] Long form (%d controls): +%d steps (budget now %d).",
            job_id, total, long_form_extra, budget,
        )
        return (
            f"\n\n[jobo] This form has {total} controls, so {long_form_extra} model steps "
            f"were added once: this job's step budget is now {budget}. Every control you "
            "type into or click yourself costs a step. Set them by number instead, text "
            "boxes too: one `NEED_FIELD:` line each, every one you can answer in ONE turn."
        )

    def progress_note(steps_used: int) -> str:
        # Near the end of the budget, a form still being filled gets one more
        # allowance (see PROGRESS_EXTENSION_STEPS).
        nonlocal budget, deadline, progress_extra
        if progress_extra or budget - steps_used > PROGRESS_LOW_WATER:
            return ""
        recent = sum(1 for at in fields_set_at.values() if at > steps_used - PROGRESS_WINDOW_STEPS)
        if recent < PROGRESS_MIN_NEW_FIELDS:
            return ""
        progress_extra = PROGRESS_EXTENSION_STEPS
        budget += PROGRESS_EXTENSION_STEPS
        deadline += PROGRESS_EXTENSION_SEC
        logger.info(
            "[%s] Still progressing (%d new fields in the last %d steps): +%d steps, +%ds (budget now %d).",
            job_id, recent, PROGRESS_WINDOW_STEPS, PROGRESS_EXTENSION_STEPS,
            PROGRESS_EXTENSION_SEC, budget,
        )
        return (
            f"\n\n[jobo] The form is still moving, so {PROGRESS_EXTENSION_STEPS} model steps "
            f"were added once: this job's step budget is now {budget}, with "
            f"{budget - steps_used} left and no more to come. Put every field you can set "
            "from one listing in the SAME turn and head for Submit."
        )

    while True:
        # A Workday apply always signs in or creates an account first, and
        # the agent usually does it by typing, with no handoff to trigger the
        # allowance: J&J 1885 (2026-10-18) spent ~12 steps creating one and
        # ran out on Voluntary Disclosures, step 4 of 5.
        if session_id and not account_extra and _seen_workday(cdp_url):
            prompt += account_setup_note()
        steps_used = sum(calls_by_session.values())
        if session_id:
            prompt += progress_note(steps_used)
        steps_left = budget - steps_used
        if session_id and steps_left <= 0:
            out = "\n".join(transcript_parts)
            out += f"\n[jobo] out of step budget ({budget} model steps) during a handoff."
            _dump_debug_transcript(job_id, out)
            logger.warning("[%s] Step budget used up (%d/%d) mid-handoff.", job_id, steps_used, budget)
            # The number in brackets stays the BASE budget: blockers.py keys
            # "escalated attempt still exhausted" on it.
            return ApplyOutcome(
                status="stuck",
                detail=(
                    f"ran out of step budget ({base_budget} model steps) mid-form, before submitting"
                    + (f" (plus {account_extra} for account setup)" if account_extra else "")
                    + (f" (plus {progress_extra} while still progressing)" if progress_extra else "")
                    + (f" (plus {long_form_extra} for a long form)" if long_form_extra else "")
                ),
            )
        remaining = int(deadline - time.monotonic())
        if remaining <= 5:
            out = "\n".join(transcript_parts)
            out += f"\n[jobo] out of time budget ({timeout_sec}s) during a handoff."
            _dump_debug_transcript(job_id, out)
            return ApplyOutcome(
                status="stuck", detail=f"agent ran out of time ({timeout_sec}s) mid-handoff"
            )

        out, timed_out = _run_hermes(
            prompt=prompt,
            cdp_url=cdp_url,
            timeout_sec=remaining,
            max_turns=str(max(1, steps_left)),
            resume_session=session_id,
            source_url=source_url,
        )
        transcript_parts.append(out)

        if timed_out:
            full_out = "\n".join(transcript_parts)
            _dump_debug_transcript(job_id, full_out)
            logger.warning("[%s] Agent hard-timeout at %ss.", job_id, timeout_sec)
            return ApplyOutcome(
                status="stuck", detail=f"agent timed out after {timeout_sec}s (no verdict printed)"
            )

        m = SESSION_ID_RE.search(out)
        if m:
            session_id = m.group(1)
        if session_id:
            calls = session_api_calls(session_id, profile)
            if calls is not None:
                calls_by_session[session_id] = calls

        # A verdict always wins over a handoff request: if the agent printed
        # STATUS it is done, whatever else is in the output.
        has_verdict = re.search(r"^STATUS:\s*\S+", out, re.MULTILINE)
        otp_req = OTP_REQUEST_RE.search(out)
        upload_req = UPLOAD_REQUEST_RE.search(out)
        field_req = FIELD_REQUEST_RE.search(out)
        # NEED_FIELDS is a bare listing request. Match it only when it is NOT
        # part of a NEED_FIELD directive line (which also starts NEED_FIELD...).
        list_req = FIELDS_LIST_RE.search(out)
        reset_req = PASSWORD_RESET_RE.search(out)
        click_req = CLICK_REQUEST_RE.search(out)
        slider_req = SLIDER_REQUEST_RE.search(out)

        if (
            (otp_req or upload_req or field_req or list_req or reset_req or click_req or slider_req)
            and not has_verdict
            and not session_id
        ):
            logger.warning(
                "[%s] Agent asked for a handoff but no session_id was captured; "
                "cannot resume.",
                job_id,
            )
            break

        # Uploads first: an agent that needs both usually needs the file
        # attached before the verification step.
        if upload_req and not has_verdict:
            if upload_rounds >= MAX_UPLOAD_ROUNDS:
                logger.warning(
                    "[%s] Agent exceeded %d upload requests; refusing further ones.",
                    job_id,
                    MAX_UPLOAD_ROUNDS,
                )
                prompt = (
                    "No further file uploads can be performed for this job "
                    "(request limit reached). Do not ask for another upload. "
                    "Continue with the remaining fields, or print your "
                    "STATUS/DETAIL/FIELDS_FILLED_JSON verdict now."
                )
                upload_rounds += 1
                continue
            upload_rounds += 1
            prompt = _resolve_upload(
                upload_req.group(0),
                cdp_url=cdp_url,
                resume_path=resume_path,
                cover_path=cover_path,
                job_id=job_id,
            )
            # A repeat request for a file the site already shows uploads
            # nothing, so it must not use up the cap (job 1266 burned its last
            # rounds re-asking for a resume Greenhouse was already showing).
            # Refunds are capped so a looping agent still gets cut off.
            if prompt.startswith(UPLOAD_ALREADY_PREFIX) and already_refunds < MAX_ALREADY_REFUNDS:
                upload_rounds -= 1
                already_refunds += 1
            continue

        if field_req and not has_verdict:
            # Every NEED_FIELD line in the turn is run, in order, and answered
            # in one resume (GHO-15: 134 of 195 field rounds on 2026-10-11/14
            # came straight after another, each a hermes restart plus a model
            # call). Each set is checked against the listing on its own, so a
            # number made stale by an earlier set fails safely.
            replies = []
            form_changed = False
            lines = list(dict.fromkeys(m.group(0) for m in FIELD_REQUEST_RE.finditer(out)))
            for i, line in enumerate(lines):
                if field_rounds >= MAX_FIELD_ROUNDS:
                    logger.warning(
                        "[%s] Agent exceeded %d field-set requests; refusing further ones.",
                        job_id,
                        MAX_FIELD_ROUNDS,
                    )
                    replies.append(
                        "No further field-set requests can be performed for this job "
                        "(request limit reached). Fill any remaining fields yourself, "
                        "or print your STATUS/DETAIL/FIELDS_FILLED_JSON verdict now."
                    )
                    field_rounds += 1
                    break
                field_rounds += 1
                reply = _resolve_field(line, cdp_url=cdp_url, job_id=job_id, failures=field_failures)
                replies.append(reply)
                new_labels = [x for x in _FIELD_SET_LABEL_RE.findall(reply) if x not in fields_set_at]
                for label in new_labels:
                    fields_set_at.setdefault(label, sum(calls_by_session.values()))
                if new_labels and new_field_refunds < MAX_NEW_FIELD_REFUNDS:
                    field_rounds -= 1
                    new_field_refunds += 1
                if FIELD_MOVED_NOTE in reply:
                    form_changed = True
                elif FORM_CHANGED_NOTE in reply:
                    form_changed = True
                    if stale_refunds < MAX_STALE_REFUNDS:
                        field_rounds -= 1
                        stale_refunds += 1
                    held = lines[i + 1:]
                    if held:
                        logger.info(
                            "[%s] Form changed mid-batch; holding back %d stale field set(s).",
                            job_id,
                            len(held),
                        )
                        replies.append(
                            "NOT RUN, because their numbers are stale now:\n"
                            + "\n".join(f"  {h.strip()}" for h in held)
                        )
                    break
            if form_changed and auto_relists < MAX_STALE_REFUNDS:
                # Fresh numbers in the same resume, instead of the agent
                # spending a turn (and a list round) asking for them.
                auto_relists += 1
                listing = _resolve_fields_listing(cdp_url=cdp_url, job_id=job_id)
                listing += long_form_note(listing)
                replies.append(
                    "The form changed while these fields were being set, so it was "
                    "re-listed for you below. Use ONLY these new numbers from now on "
                    "(the old ones are stale); do not print NEED_FIELDS for this.\n\n"
                    + listing
                )
            prompt = "\n\n".join(replies)
            continue

        # Listing is checked AFTER NEED_FIELD so a directive line always wins
        # (both start with the same prefix).
        if list_req and not has_verdict:
            if field_list_rounds >= MAX_FIELD_LIST_ROUNDS:
                logger.warning(
                    "[%s] Agent exceeded %d field-list requests.",
                    job_id,
                    MAX_FIELD_LIST_ROUNDS,
                )
                prompt = (
                    "No further field listings can be provided for this job "
                    "(request limit reached). Fill the remaining fields with the "
                    "numbers you already have, or print your "
                    "STATUS/DETAIL/FIELDS_FILLED_JSON verdict now."
                )
                field_list_rounds += 1
                continue
            field_list_rounds += 1
            start = _LIST_START_RE.search(list_req.group(0))
            prompt = _resolve_fields_listing(
                cdp_url=cdp_url, job_id=job_id, start=int(start.group(1)) if start else 1)
            prompt += long_form_note(prompt)
            continue

        # Before NEED_CLICK: an agent that asks for both wants the captcha
        # verified before it clicks Submit.
        if slider_req and not has_verdict:
            if slider_rounds >= MAX_SLIDER_ROUNDS:
                logger.warning("[%s] Agent exceeded %d slider requests.", job_id, MAX_SLIDER_ROUNDS)
                prompt = (
                    "No further slider solves can be performed for this job (request "
                    "limit reached). Do not drag the slider yourself. Print your "
                    "STATUS/DETAIL/FIELDS_FILLED_JSON verdict now (captcha_failed if the "
                    "captcha is still not verified)."
                )
                slider_rounds += 1
                continue
            slider_rounds += 1
            prompt = _resolve_slider(cdp_url=cdp_url, job_id=job_id)
            continue

        if click_req and not has_verdict:
            if click_rounds >= MAX_CLICK_ROUNDS:
                logger.warning(
                    "[%s] Agent exceeded %d click requests.", job_id, MAX_CLICK_ROUNDS
                )
                prompt = (
                    "No further clicks can be performed for this job (request "
                    "limit reached). Print your STATUS/DETAIL/FIELDS_FILLED_JSON "
                    "verdict now."
                )
                click_rounds += 1
                continue
            click_rounds += 1
            prompt = _resolve_click(click_req.group(0), cdp_url=cdp_url, job_id=job_id)
            # A refused submit (Easy Apply or any form) sent no click; don't
            # let the agent's fix-and-retry cycle eat the small click cap (bounded).
            if "Submit refused" in prompt and resume_refusals < MAX_RESUME_REFUSALS:
                click_rounds -= 1
                resume_refusals += 1
            if _ACCOUNT_SETUP_CLICK.search(click_req.group(0)):
                prompt += account_setup_note()
            continue

        if reset_req and not has_verdict:
            if reset_rounds >= MAX_PASSWORD_RESET_ROUNDS:
                logger.warning(
                    "[%s] Agent exceeded %d password-reset requests.",
                    job_id,
                    MAX_PASSWORD_RESET_ROUNDS,
                )
                prompt = (
                    "No further password resets can be performed for this job "
                    "(request limit reached). If you still cannot sign in, print "
                    "your STATUS/DETAIL/FIELDS_FILLED_JSON verdict now."
                )
                reset_rounds += 1
                continue
            reset_rounds += 1
            prompt = _resolve_password_reset(reset_req.group(0), job_id=job_id)
            prompt += account_setup_note()
            continue

        if otp_req and not has_verdict:
            if otp_rounds >= MAX_OTP_ROUNDS:
                logger.warning(
                    "[%s] Agent exceeded %d OTP requests; giving up on the handoff.",
                    job_id,
                    MAX_OTP_ROUNDS,
                )
                prompt = (
                    "No further verification codes can be retrieved for this job "
                    "(request limit reached). Do not ask for another code. If you "
                    "cannot proceed, print your STATUS/DETAIL/FIELDS_FILLED_JSON "
                    "verdict now."
                )
                otp_rounds += 1
                continue
            otp_rounds += 1
            otp_seen: dict = {}
            code, note = _resolve_otp(
                otp_req.group("sender") or "",
                (otp_req.group("hint") or "").strip(),
                job_id,
                not_before=otp_not_before,
                seen=otp_seen,
                company=company,
                employer_urls=tuple(u for u in (source_url,) if u),
                cdp_url=cdp_url,
            )
            if code and isinstance(otp_seen.get("received_at"), datetime):
                otp_not_before = max(
                    otp_not_before, otp_seen["received_at"] + timedelta(seconds=1)
                )
            if code and code in otp_codes_given:
                logger.warning(
                    "[%s] Lookup returned a code this job was already given; not handing it over again.",
                    job_id,
                )
                prompt = (
                    "No new verification code could be retrieved: the newest mail "
                    "carries the same code you were already given, which the page did "
                    "not accept. Do not enter it again. If the page offers to resend "
                    "the code, do that once and print NEED_OTP again. Otherwise print "
                    "your STATUS/DETAIL/FIELDS_FILLED_JSON verdict now with the reason."
                )
            elif code:
                otp_codes_given.add(code)
                prompt = (
                    f"VERIFICATION_CODE: {code}\n\n"
                    "That is the code that was just emailed. Enter it in the "
                    "verification field on the page and continue the application "
                    "through to submission. Do not restart the form - your browser "
                    "session is still on the same page."
                )
                # An emailed code or link is account setup too, and it often
                # comes late: FedEx 2244 (Paradox chat, 2026-10-25) got its code
                # after a long chat intake and ran out at 80/80 two digits in,
                # before the form behind it.
                prompt += account_setup_note()
            elif note.startswith("REFUSED: "):
                otp_rounds = MAX_OTP_ROUNDS
                prompt = (
                    f"OTP_REFUSED: {note[len('REFUSED: '):]}\n\n"
                    "Do not request another code and do not continue any LinkedIn "
                    "or Google sign-in. Print your verdict now."
                )
            elif note.startswith("BLOCKER: "):
                # The portal told us no code is coming. Burn no more rounds on
                # it: force the agent onto a different tactic immediately.
                otp_rounds = MAX_OTP_ROUNDS
                prompt = (
                    f"NO_CODE_WILL_ARRIVE: {note[len('BLOCKER: '):]}\n\n"
                    "Do not request another verification code for this job - the "
                    "portal has already told us one will not be sent."
                )
            else:
                prompt = (
                    f"No verification code could be retrieved ({note}). "
                    "Try resending the code from the page if that option exists, "
                    "and if so print NEED_OTP again. Otherwise print your "
                    "STATUS/DETAIL/FIELDS_FILLED_JSON verdict now with the reason."
                )
            continue

        break

    full_out = "\n".join(transcript_parts)
    outcome = parse_verdict(full_out)
    logger.info("[%s] Model steps used: %d of %d.", job_id, sum(calls_by_session.values()), budget)
    if otp_rounds or upload_rounds or field_rounds or field_list_rounds or click_rounds or slider_rounds:
        logger.info(
            "[%s] Handoff rounds used: otp=%d upload=%d field=%d field_list=%d click=%d slider=%d",
            job_id,
            otp_rounds,
            upload_rounds,
            field_rounds,
            field_list_rounds,
            click_rounds,
            slider_rounds,
        )
    if outcome.status != "submitted":
        _dump_debug_transcript(job_id, full_out)
    logger.info("Agent verdict: %s (%s)", outcome.status, outcome.detail[:120])
    return outcome
