"""Verify tailored resume.tex only touched Skills/Projects sections.

The MATERIALS_SYSTEM prompt (module3/prompts.py) tells the LLM to copy
Experience, Education, and the heading verbatim from the base LaTeX and only
edit Skills/Projects. LLMs don't always follow instructions perfectly -
this module checks the actual output rather than trusting the prompt alone.
"""

from __future__ import annotations

import re

# Section headings in resume.tex, in document order. Everything between one
# \section{...} and the next belongs to that section.
_SECTION_RE = re.compile(r"\\section\{([^}]+)\}")

# Sections that MUST be byte-identical between base and tailored LaTeX.
LOCKED_SECTIONS = {"Education", "Experience", "Achievements & Leadership", "Achievements \\& Leadership"}


def find_em_dashes(tex: str) -> int:
    """Count em-dash (—) occurrences anywhere in the document.

    the user's standing preference: never use them, period - confirmed 2026-09-02
    after he flagged 2 em dashes that had crept into his own supplied base
    resume as a mistake he wanted corrected. The base template itself must
    now be em-dash-free too, so this checks the whole document rather than
    only the LLM-editable sections (Skills/Projects) as an earlier version
    of this function did - that scoping was only ever a workaround for a
    base file that legitimately (if unintentionally) had em dashes in its
    locked Experience section; now that the base is clean, the whole-
    document check is both simpler and correct.
    """
    return tex.count("—")


def _split_sections(tex: str) -> dict[str, str]:
    """Map section name -> its full body text (from its \\section{} to the next)."""
    matches = list(_SECTION_RE.finditer(tex))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tex)
        out[name] = tex[start:end]
    return out


def find_locked_section_violations(base_tex: str, tailored_tex: str) -> list[str]:
    """Return a list of human-readable violation descriptions; empty = clean."""
    base_sections = _split_sections(base_tex)
    tailored_sections = _split_sections(tailored_tex)
    violations = []
    for name in LOCKED_SECTIONS:
        if name not in base_sections:
            continue
        if name not in tailored_sections:
            violations.append(f"Locked section {name!r} is MISSING from tailored resume.")
            continue
        # rstrip: the LAST section in the document runs to end-of-file, so a
        # missing/extra trailing newline (a byte the LLM has no real control
        # over - it's an artifact of how the base file was saved) must not
        # register as a content violation. Only real content differences count.
        if base_sections[name].rstrip() != tailored_sections[name].rstrip():
            violations.append(
                f"Locked section {name!r} was modified (should be copied verbatim)."
            )
    # Also check the heading/contact-info preamble (everything before the
    # first \section{}) is untouched - name, email, phone, links.
    base_preamble = base_tex[: base_tex.find("\\section{")] if "\\section{" in base_tex else base_tex
    tailored_preamble = (
        tailored_tex[: tailored_tex.find("\\section{")] if "\\section{" in tailored_tex else tailored_tex
    )
    if base_preamble.strip() != tailored_preamble.strip():
        violations.append("Heading/contact-info preamble was modified (should be copied verbatim).")
    return violations
