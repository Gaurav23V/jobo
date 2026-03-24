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
    """Concatenate project sections for the given names; log and skip unknown."""
    sections = parse_project_sections(markdown)
    parts: list[str] = []
    for raw in names:
        key = raw.strip()
        if not key:
            continue
        if key not in sections:
            logger.warning(
                "Profile has no ## section titled %r; skipping that project block.",
                key,
            )
            continue
        parts.append(f"## {key}\n\n{sections[key]}")
    return "\n\n---\n\n".join(parts)
