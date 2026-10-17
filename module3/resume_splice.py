"""Build the tailored resume by splicing new Skills/Projects blocks into the base.

The materials call used to return the whole LaTeX document, with a prompt
begging the model to copy Experience/Education/heading byte-for-byte. It
sometimes didn't (resume_guard caught it and forced a retry), and it cost
~2.5k output tokens per job to echo back text that must not change. Now the
model returns only the two editable blocks and this module pastes them into
the base template, so the locked sections cannot be touched at all.
"""

from __future__ import annotations

import re

# Section titles exactly as they appear in \section{...} in the base resume.
SKILLS_SECTION = "Programming Skills"
PROJECTS_SECTION = "Projects"
EDITABLE_SECTIONS = (SKILLS_SECTION, PROJECTS_SECTION)

_SECTION_RE = re.compile(r"\\section\{([^}]+)\}")


def _block_span(tex: str, name: str) -> tuple[int, int]:
    """(start, end) of a section's block: from its \\section{} to its last
    content line, excluding trailing blank lines and %---- separator comments
    that introduce the next section."""
    matches = list(_SECTION_RE.finditer(tex))
    for i, m in enumerate(matches):
        if m.group(1) != name:
            continue
        start = m.start()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            doc_end = tex.find("\\end{document}", start)
            end = doc_end if doc_end != -1 else len(tex)
        lines = tex[start:end].split("\n")
        while len(lines) > 1 and (not lines[-1].strip() or lines[-1].lstrip().startswith("%")):
            lines.pop()
        return start, start + len("\n".join(lines))
    raise ValueError(f"base resume has no \\section{{{name}}}")


def editable_blocks(base_tex: str) -> dict[str, str]:
    """The current Skills and Projects blocks of the base resume."""
    out = {}
    for name in EDITABLE_SECTIONS:
        s, e = _block_span(base_tex, name)
        out[name] = base_tex[s:e]
    return out


def normalize_block(name: str, block: str) -> str:
    """Validate one model-supplied block and return it cleaned up.

    Raises ValueError with a message meant to be fed back to the model.
    """
    text = (block or "").strip()
    # Strip markdown fences the model may wrap around LaTeX.
    text = re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", text).strip()
    header = f"\\section{{{name}}}"
    if not text.startswith("\\section{"):
        text = f"{header}\n{text}"
    sections = _SECTION_RE.findall(text)
    if sections != [name]:
        raise ValueError(
            f"The {name} block must contain exactly one \\section, "
            f"\\section{{{name}}}; got {sections}."
        )
    for forbidden in ("\\documentclass", "\\begin{document}", "\\end{document}"):
        if forbidden in text:
            raise ValueError(f"The {name} block must not contain {forbidden}.")
    # Em dashes are never wanted (see resume_guard.find_em_dashes). Replace
    # them here instead of spending a whole retry call on it.
    return text.replace("—", ", ")


_PROJECT_ENTRY = "\\resumeSubItem{"
_LIST_END = "\\resumeSubHeadingListEnd"


def count_projects(block: str) -> int:
    """Number of project entries (\\resumeSubItem) in a Projects block."""
    return (block or "").count(_PROJECT_ENTRY)


def trim_projects(block: str, keep: int) -> str:
    """Return the Projects block with only its first `keep` entries.

    The base resume's page is full with its own project count; one extra
    project (3-4 lines) pushes the whole last section onto page 2, and no
    amount of rewording wins that much room back. The prompt asks for the
    most relevant project first, so the entries dropped are the last ones.
    Entries sit back to back inside the list, so everything from the start
    of entry `keep + 1` up to the list's end is cut.
    """
    starts = [m.start() for m in re.finditer(re.escape(_PROJECT_ENTRY), block)]
    if keep < 1 or len(starts) <= keep:
        return block
    end = block.rfind(_LIST_END)
    if end == -1 or end < starts[keep]:
        raise ValueError("Projects block has no closing \\resumeSubHeadingListEnd")
    return block[: starts[keep]].rstrip() + "\n  " + block[end:]


def splice(base_tex: str, blocks: dict[str, str]) -> str:
    """Return base_tex with each non-empty block replacing its section.

    An empty or missing block keeps the base section as-is.
    """
    tex = base_tex
    # Replace from the end of the document backwards so earlier spans stay valid.
    spans = sorted(
        ((name, _block_span(base_tex, name)) for name in EDITABLE_SECTIONS),
        key=lambda item: item[1][0],
        reverse=True,
    )
    for name, (s, e) in spans:
        new = (blocks.get(name) or "").strip()
        if not new:
            continue
        tex = tex[:s] + normalize_block(name, new) + tex[e:]
    return tex
