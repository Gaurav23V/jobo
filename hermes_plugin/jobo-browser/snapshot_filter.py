"""Shrink Hermes browser snapshots to what a form-filling agent acts on.

Why: on the 2026-09 apply runs, page snapshots were ~59% of every token the
apply agent spent. Each one is the page's whole accessibility tree (~11k
chars, ~4.5k tokens), and because every model call re-sends the full history,
a snapshot taken at step 3 is paid for again at steps 4, 5, ... 60. Most of
that tree is job-description prose, list markers and empty wrappers.

The browser engine (agent-browser) already has an "interactive elements only"
mode, but Hermes never passes it. Measured on real forms it is 3-9x smaller
(Greenhouse 20,975 -> 2,413 chars). This filter gives the same view from the
compact tree Hermes already returns, and keeps a little more:

  * every line carrying a ref (anything the agent can click or type into),
    with its accessible name - which is where the field label and the full
    question text live
  * headings, dialogs, alerts and status regions (where am I, did it work)
  * static text that reads like an error, a requirement or a confirmation
    ("required", "invalid", "thank you", "submitted", "no longer accepting")

Long option lists (a 250-country <select>) are cut to the first few options.
browser_snapshot(full=true) is never filtered, so the agent can always read
the whole page when it needs to.

Pure functions only, so jobo's tests can import this file directly.
"""

from __future__ import annotations

import json
import re

MARKER = (
    "[jobo: form view - interactive elements, headings and status text only. "
    "Call browser_snapshot with full=true to read the page's full text.]"
)

# Roles kept even without a ref: they orient the agent or report outcomes.
_ALWAYS_KEEP_ROLES = {
    "heading", "dialog", "alertdialog", "alert", "status",
    "group", "radiogroup", "tablist", "form", "legend", "iframe",
}
_TEXT_ROLES = {"StaticText", "paragraph", "LabelText", "generic", "strong", "emphasis", "text"}
_SIGNAL_RE = re.compile(
    r"\brequired\b|\binvalid\b|\berror\b|\bfailed\b|"
    r"please (?:enter|select|fill|provide|complete|upload|choose|correct)|"
    r"thank you|thanks for applying|application (?:has been |was )?(?:submitted|received|sent)|"
    r"successfully (?:submitted|applied|sent|received)|already applied|"
    r"no longer (?:accepting|available)|not (?:currently )?accepting|"
    r"\bverif(?:y|ication)\b|(?:enter|sent|resend) (?:the |a |your )?(?:code|otp)|"
    r"captcha|not a robot|step \d+ of \d+|page \d+ of \d+",
    re.I,
)
# Status text is short; job-description prose that happens to contain
# "required" or "verify" is long. Only short text lines are kept.
MAX_SIGNAL_TEXT_CHARS = 160
_LINE_RE = re.compile(r"^(\s*)- (\S+)(.*)$")
MAX_OPTIONS = 12
# Below this the filter is not worth the risk of hiding something.
MIN_FILTER_CHARS = 1500


def filter_snapshot(snapshot: str) -> str:
    """Return the reduced view of an agent-browser compact snapshot."""
    out: list[str] = []
    option_run = 0
    skipped_options = 0

    def flush_options() -> None:
        nonlocal skipped_options
        if skipped_options:
            out.append(f"  - ... {skipped_options} more options (type to search, or full=true to list)")
            skipped_options = 0

    for line in snapshot.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        _indent, role, rest = m.groups()
        has_ref = "ref=e" in rest
        if role == "option":
            option_run += 1
            if option_run > MAX_OPTIONS:
                skipped_options += 1
                continue
        else:
            flush_options()
            option_run = 0

        keep = False
        if has_ref or role == "option":
            keep = True
        elif role in _ALWAYS_KEEP_ROLES and (rest.strip() or role in {"dialog", "alertdialog", "alert"}):
            keep = True
        elif role in _TEXT_ROLES and len(rest) <= MAX_SIGNAL_TEXT_CHARS and _SIGNAL_RE.search(rest):
            keep = True
        if keep:
            # Flat list like agent-browser's own `snapshot -i`: indentation
            # costs tokens and the refs already identify each element.
            out.append(f"- {role}{rest}")
    flush_options()
    return "\n".join([MARKER, *out])


# Hermes cuts a snapshot over `browser.snapshot_threshold` (15k chars) at a line
# boundary, stores the whole tree on disk and appends this note. Long Avature
# forms (Wingtip 40k, Southgate 19-21k chars) are cut right after the first
# 250-option <select>, so the cut view ends at "Country".
#
# Measured on Wingtip 1318 and Southgate 1419 (2026-10-03): `filter_snapshot` dropped
# the note too, because it is not a "- role" line. 45 of the 53 compact
# snapshots on those forms ended at Country with no sign that anything followed.
# Password, the consent box and Continue never appeared, and the agents spent
# their steps on scrolls and console probes looking for them. Filtering the
# STORED full tree instead fits easily: Wingtip 40,351 -> 9,934 chars, Southgate
# 21,388 -> 2,614.
_TRUNC_NOTE_RE = re.compile(
    r"\n*\[\.\.\. (?P<n>\d+) more lines truncated[^\n\]]*?"
    r'(?:full snapshot: read_file path="(?P<path>[^"\n]+)"[^\n\]]*)?\]\s*$'
)
_STORED_NAME_RE = re.compile(r"^browser-snapshot-[0-9a-f]{6,64}\.txt$")
# A filtered full tree larger than this is not inlined; the cut view plus an
# explicit "the page continues" line is returned instead.
MAX_EXPANDED_CHARS = 16000
MAX_STORED_READ_CHARS = 2_000_000


def _stored_snapshot(path: str) -> str | None:
    """Read a snapshot file Hermes stored, and nothing else.

    Only `.../cache/web/browser-snapshot-<hex>.txt` is accepted (the path
    Hermes's own _store_full_snapshot writes), so text on a web page that
    merely looks like the note cannot make the plugin read an arbitrary file.
    """
    from pathlib import Path

    try:
        p = Path(path)
        if not p.is_absolute() or not _STORED_NAME_RE.match(p.name):
            return None
        if p.parent.name != "web" or p.parent.parent.name != "cache":
            return None
        if not p.is_file() or p.is_symlink():
            return None
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_STORED_READ_CHARS)
    except OSError:
        return None


def reduce_snapshot(snap: str) -> str:
    """Filtered view of a (possibly Hermes-truncated) snapshot.

    If Hermes cut the tree and stored it, filter the stored full tree. If that is
    unavailable or still too big, keep the cut view and SAY the page continues
    (never drop the truncation note silently).
    """
    m = _TRUNC_NOTE_RE.search(snap)
    if not m:
        return filter_snapshot(snap)
    head = snap[: m.start()]
    stored = _stored_snapshot(m.group("path") or "")
    if stored:
        full = filter_snapshot(stored)
        if len(full) <= MAX_EXPANDED_CHARS:
            return full
    reduced = filter_snapshot(head)
    hint = (
        f"[jobo: the page CONTINUES below - {m.group('n')} more lines were cut from this view. "
        "Use NEED_FIELDS for a numbered list of every form control, or browser_scroll "
        "down and snapshot again.]"
    )
    return f"{reduced}\n{hint}"


def transform_result(tool_name: str, args: dict | None, result: str) -> str | None:
    """Rewrite a browser_snapshot / browser_navigate JSON result, or return None to leave it."""
    if tool_name not in ("browser_snapshot", "browser_navigate"):
        return None
    if tool_name == "browser_snapshot" and bool((args or {}).get("full")):
        return None
    if not isinstance(result, str) or '"snapshot"' not in result:
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    snap = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(snap, str) or len(snap) < MIN_FILTER_CHARS:
        return None
    reduced = reduce_snapshot(snap)
    # An expanded view may be longer than the cut one, and that is the point:
    # the cut one was missing the bottom of the form.
    if len(reduced) >= len(snap) and not _TRUNC_NOTE_RE.search(snap):
        return None
    data["snapshot"] = reduced
    return json.dumps(data, ensure_ascii=False)
