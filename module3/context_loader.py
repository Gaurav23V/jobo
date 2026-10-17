"""Load profile.md and extract ## project sections by heading text."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Lines like "## Some Project Name" (level-2 heading only).
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def load_profile_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Profile context not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def get_introduction_markdown(markdown: str) -> str:
    """Return body under ``# Introduction`` until the next top-level ``# ...`` heading (not ``##``)."""
    m = re.search(r"(?ms)^#\s+Introduction\s*\n(.*)", markdown)
    if not m:
        return ""
    body = m.group(1)
    next_h1 = re.search(r"(?m)^#\s+.+\s*$", body)
    if next_h1:
        body = body[: next_h1.start()]
    return body.strip()


def parse_project_sections(markdown: str) -> dict[str, str]:
    """Map ## heading text -> body until next ## or EOF.

    Convention: non-project content uses # only; each project is ## Title.
    """
    matches = list(_H2_RE.finditer(markdown))
    if not matches:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        out[title] = body
    return out


def list_project_names(markdown: str) -> list[str]:
    return list(parse_project_sections(markdown).keys())


def get_project_bodies(markdown: str, names: list[str]) -> str:
    """Concatenate project sections for the given names; log and skip unknown.

    Matching is deliberately forgiving. The names come from an LLM that read
    the profile, and it routinely returns a different CASE or punctuation than
    the literal heading - observed 2026-09-03: it asked for 'Budget Bot' when the
    heading is '## budget-bot', and 'RelayDelivery Service' for
    '## relay-service'. An exact-match-only lookup silently dropped those
    projects from the tailored resume, which is a content-quality bug that
    only shows up as a WARNING in the log.

    Order of attempts: exact -> case-insensitive -> normalized (alphanumeric
    only, so hyphens/spaces/underscores stop mattering) -> unique substring.
    """
    sections = parse_project_sections(markdown)
    lower_map = {k.lower(): k for k in sections}

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def _tokens(s: str) -> set[str]:
        """Word-ish tokens, also splitting camelCase so 'RelayDelivery'
        yields {'relay', 'delivery'}."""
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
        return {t for t in re.findall(r"[a-z0-9]+", spaced.lower()) if len(t) > 2}

    norm_map: dict[str, list[str]] = {}
    for k in sections:
        norm_map.setdefault(_norm(k), []).append(k)

    parts: list[str] = []
    used: set[str] = set()
    for raw in names:
        key = raw.strip()
        if not key:
            continue

        resolved = None
        if key in sections:
            resolved = key
        elif key.lower() in lower_map:
            resolved = lower_map[key.lower()]
        else:
            candidates = norm_map.get(_norm(key)) or []
            if len(candidates) == 1:
                resolved = candidates[0]
            else:
                # Tier 3: substring either way, accepted only when unambiguous.
                nk = _norm(key)
                subs = {
                    orig
                    for n, originals in norm_map.items()
                    for orig in originals
                    if nk and (nk in n or n in nk)
                }
                if len(subs) == 1:
                    resolved = next(iter(subs))
                else:
                    # Tier 4: token overlap across every section. Handles names
                    # that are neither a substring nor a case variant, e.g.
                    # 'RelayDelivery Service' -> '## relay-service'.
                    # Only accepted when there is a STRICTLY best candidate, so
                    # an ambiguous name is skipped rather than mis-attached.
                    pool = subs or set(sections)
                    key_tokens = _tokens(key)
                    if key_tokens:
                        scored: list[tuple[float, str]] = []
                        for cand in pool:
                            cand_tokens = _tokens(cand)
                            if not cand_tokens:
                                continue
                            shared = key_tokens & cand_tokens
                            if not shared:
                                continue
                            # Jaccard-ish: reward overlap, penalise candidates
                            # carrying lots of unrelated tokens.
                            score = len(shared) / len(key_tokens | cand_tokens)
                            scored.append((score, cand))
                        scored.sort(key=lambda t: (-t[0], t[1]))
                        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                            resolved = scored[0][1]

        if not resolved:
            logger.warning(
                "Profile has no ## section matching %r (tried exact, case-insensitive, "
                "normalized and substring); skipping that project block.",
                key,
            )
            continue
        if resolved != key:
            logger.info("Resolved requested project %r to profile section %r.", key, resolved)
        # Two names for the same project (seen live 2026-09-28: 'RelayDelivery
        # Service' and 'relay-service') must not send its body twice.
        if resolved in used:
            continue
        used.add(resolved)
        parts.append(f"## {resolved}\n\n{sections[resolved]}")
    return "\n\n---\n\n".join(parts)


# How much of each project's description the fit call sees. The fit call only
# has to decide "is this job a match, and which projects are relevant", so a
# stack line plus the opening sentence is enough; the full project body is
# still sent to the materials call for the projects it picks.
_FIT_SUMMARY_MAX_CHARS = 180

_FIELD_RE = re.compile(r"^-\s+\*\*(\w[\w ]*):\*\*\s*(.*)$", re.MULTILINE)


def _first_sentence(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"(?<=[.!?])\s", text)
    if m and m.start() <= limit:
        return text[: m.start()]
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def build_fit_profile(markdown: str) -> str:
    """Compact profile for the fit call: everything before ``# Projects`` kept
    verbatim, each project reduced to one line (name, stack, first sentence).

    The full profile is ~94KB, almost all of it 71 project write-ups. Sending
    it whole on every fit call cost ~25k tokens per job for detail the fit
    decision never used.
    """
    m = re.search(r"(?m)^#\s+Projects\s*$", markdown)
    if not m:
        return markdown
    head = markdown[: m.start()].rstrip()
    lines = [
        "# Projects (index)",
        "",
        "One line per project: `name` | stack | summary. The name in backticks "
        "is the exact project name.",
        "",
    ]
    for name, body in parse_project_sections(markdown[m.start():]).items():
        fields = {k.strip().lower(): v.strip() for k, v in _FIELD_RE.findall(body)}
        stack = fields.get("stack", "")
        desc = fields.get("description") or body
        summary = _first_sentence(desc, _FIT_SUMMARY_MAX_CHARS)
        parts = [f"`{name}`"] + [p for p in (stack, summary) if p]
        lines.append("- " + " | ".join(parts))
    return head + "\n\n" + "\n".join(lines) + "\n"
