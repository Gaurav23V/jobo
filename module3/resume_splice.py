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
from dataclasses import dataclass

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


_HREF_RE = re.compile(r"\\href\{[^}]*\}")
_COLOR_RE = re.compile(r"\\color\{[^}]*\}")
_CMD_RE = re.compile(r"\\[A-Za-z]+\*?(\[[^\]]*\])?")


def printed_text(latex: str) -> str:
    """Roughly what a LaTeX fragment prints: commands and braces gone,
    escapes like \\& kept as their character, whitespace collapsed."""
    text = _HREF_RE.sub("", latex)
    text = _COLOR_RE.sub("", text)
    text = text.replace("\\textbar", "|").replace("\\ ", " ").replace("~", " ")
    text = re.sub(r"\\([&%$#_])", r"\1", text)
    text = _CMD_RE.sub("", text)
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


def _items(block: str) -> list[tuple[int, int]]:
    """(start, end) spans of each `\\item` in a block, each ending where the
    next \\item, \\end{itemize} or \\resumeSubHeadingListEnd starts."""
    spans = []
    for m in re.finditer(r"\\item\b", block):
        s = m.start()
        ends = [i for i in (block.find("\\item", s + 5), block.find("\\end{itemize}", s),
                            block.find(_LIST_END, s)) if i != -1]
        spans.append((s, min(ends) if ends else len(block)))
    return spans


def item_texts(block: str) -> list[str]:
    """Printed text of each `\\item` in a block: the Skills lines of the
    Skills block, the bullets of the Projects block."""
    return [printed_text(block[s + len("\\item"):e]) for s, e in _items(block)]


@dataclass(frozen=True)
class LineBudget:
    """How much text the base page holds in the two editable sections.

    The base resume fills its page to within one printed line (7.7pt spare
    out of 722pt in Oct 2026), so a tailored block fits only if every Skills
    line still prints on one line and no project bullet prints on more lines
    than the base bullets do. The longest base line of each kind is known to
    fit, so it is the limit for every line of that kind.
    """

    skills_lines: int
    skills_chars: int
    bullets: int
    bullet_chars: int


def line_budget(base_tex: str) -> LineBudget | None:
    blocks = editable_blocks(base_tex)
    skills = item_texts(blocks[SKILLS_SECTION])
    bullets = item_texts(blocks[PROJECTS_SECTION])
    if not skills or not bullets:
        return None
    return LineBudget(
        skills_lines=len(skills),
        skills_chars=max(len(s) for s in skills),
        bullets=len(bullets),
        bullet_chars=max(len(b) for b in bullets),
    )


def over_budget(skills: str, projects: str, budget: LineBudget) -> list[str]:
    """One message per Skills line or project bullet over the budget, naming
    the line so a retry fixes that line. Empty blocks are the base ones."""
    problems = []
    lines = item_texts(skills) if skills.strip() else []
    if len(lines) > budget.skills_lines:
        problems.append(f"Skills has {len(lines)} lines; the page holds {budget.skills_lines}.")
    for line in lines:
        if len(line) > budget.skills_chars:
            problems.append(
                f'Skills line "{line[:30]}..." prints {len(line)} characters; '
                f"max {budget.skills_chars}."
            )
    bullets = item_texts(projects) if projects.strip() else []
    if len(bullets) > budget.bullets:
        problems.append(f"Projects has {len(bullets)} bullets; the page holds {budget.bullets}.")
    for bullet in bullets:
        if len(bullet) > budget.bullet_chars:
            problems.append(
                f'Projects bullet "{bullet[:30]}..." prints {len(bullet)} characters; '
                f"max {budget.bullet_chars}."
            )
    return problems


def _drop_last_entry(item: str) -> str | None:
    """The Skills item with its last comma-separated entry removed, or None
    when one entry is left. Commas inside parentheses, as in
    "AWS (ECS, Lambda)", belong to one entry."""
    depth = paren = 0
    commas = []
    for i, ch in enumerate(item):
        if ch in "{}" and i and item[i - 1] == "\\":
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
        elif ch == "," and paren == 0:
            commas.append((i, depth))
    if not commas:
        return None
    cut, level = commas[-1]
    # The entry ends where its brace group closes ({: a, b, c}), else at the
    # end of the item's text.
    end = len(item.rstrip())
    depth = level
    for j in range(cut, len(item)):
        if item[j] in "{}" and item[j - 1] == "\\":
            continue
        if item[j] == "{":
            depth += 1
        elif item[j] == "}":
            if depth == level:
                end = j
                break
            depth -= 1
    return item[:cut] + item[end:]


def trim_skills(block: str, max_chars: int) -> str:
    """Drop entries from the end of every Skills line that prints longer than
    max_chars until it fits.

    The commonest first-try overflow: the model adds an entry or two to a
    Skills line that already fills its printed line, the line wraps and the
    full page spills onto page 2. The prompt asks for the most relevant
    entries first, so the ones dropped are the last.
    """
    out = []
    pos = 0
    for s, e in _items(block):
        item = block[s:e]
        while len(printed_text(item[len("\\item"):])) > max_chars:
            shorter = _drop_last_entry(item)
            if shorter is None:
                break
            item = shorter
        out.append(block[pos:s] + item)
        pos = e
    return "".join(out) + block[pos:]


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
