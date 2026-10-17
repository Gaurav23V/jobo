#!/usr/bin/env python3
"""Create personal/ from a pre-public jobo checkout's hard-coded values.

Before jobo went public, the candidate's details were written into the code
(module4/prompts.py, module4/password_reset.py, module4/upload_names.py,
module4/answer_book.py, policy/employer_blocklist.py) and into tracked files (module3/context/profile.md,
module3/templates/resume.tex/.pdf). The public code reads them from the
gitignored personal/ directory instead (policy/personal.py).

This helper reads those values out of an OLD git revision (they are never
copied into this script) and writes them to personal/, so an existing install
keeps behaving exactly as before:

    scripts/migrate_local_config.py --repo /path/to/old/jobo --ref main \\
        --out /path/to/old/jobo/personal

It only ADDS files: nothing in the source repo is changed or moved, and an
existing output directory is refused unless --force is given. Values are never
printed (the summary lists file names and key names only).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def git_show(repo: Path, ref: str, path: str, *, binary: bool = False) -> "str | bytes | None":
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    return out.stdout if binary else out.stdout.decode("utf-8")


def _text(repo: Path, ref: str, path: str) -> str:
    data = git_show(repo, ref, path)
    return data if isinstance(data, str) else ""


def module_constant(source: str, name: str):
    """Value of a top-level `NAME = <literal>` assignment, or None."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    return ast.literal_eval(node.value)
                except ValueError:
                    return None
    return None


def parse_facts(facts: str) -> dict:
    """Structured fields from the old CANDIDATE_FACTS text block."""
    data: dict = {}
    m = re.search(r"Name:\s*([^.\n]+?)\.\s", facts)
    if m:
        data["full_name"] = m.group(1).strip()
        data["first_name"] = data["full_name"].split()[0]
    m = re.search(r"Email:\s*([^\s]+@[^\s]+?)\.?\s", facts)
    if m:
        data["email"] = m.group(1).strip()
    m = re.search(r"Phone:\s*([+\d][\d\s-]+\d)", facts)
    if m:
        data["phone"] = m.group(1).strip()
    for key, label in (("linkedin_url", "LinkedIn"), ("github_url", "GitHub"), ("website_url", "Website")):
        m = re.search(rf"{label}:\s*(https?://\S+)", facts)
        if m:
            data[key] = m.group(1).strip()
    m = re.search(r"Location:\s*([^\n(]+)", facts)
    if m:
        data["location"] = m.group(1).strip().rstrip(".")
    return data


def answer_chains(source: str) -> dict:
    """School and city chains from the old answer_book._CHAINS.

    The school chain is the one ending in "School not listed"-style escapes
    (kept out of the options: answer_book adds them back). The city chain is
    the only other chain whose triggers are not generic vocabulary.
    """
    chains = module_constant(source, "_CHAINS") or []
    generic = {"bachelor", "computer science", "technology", "engineering", "india", "yes", "no"}
    escapes = {
        "school not listed", "college not listed", "university not listed",
        "institution not listed", "other", "other (not listed)", "not listed",
    }
    out: dict = {}
    for triggers, options in chains:
        if any(o.lower() == "school not listed" for o in options):
            out["school"] = {
                "triggers": list(triggers),
                "options": [o for o in options if o.lower() not in escapes],
            }
        elif triggers and triggers[0] not in generic and "city" not in out:
            out["city"] = {"triggers": list(triggers), "options": list(options)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, type=Path, help="old jobo checkout (read-only)")
    ap.add_argument("--ref", default="main", help="old revision holding the hard-coded values")
    ap.add_argument("--out", required=True, type=Path, help="personal/ directory to create")
    ap.add_argument("--force", action="store_true", help="overwrite files in an existing --out")
    args = ap.parse_args(argv)

    repo, ref, out = args.repo.resolve(), args.ref, args.out
    prompts = _text(repo, ref, "module4/prompts.py")
    if not prompts:
        print(f"error: {ref}:module4/prompts.py not found in {repo}", file=sys.stderr)
        return 2
    facts = module_constant(prompts, "CANDIDATE_FACTS")
    if not isinstance(facts, str) or "Name:" not in facts:
        print(f"error: {ref} has no hard-coded CANDIDATE_FACTS (already migrated?)", file=sys.stderr)
        return 2

    if out.exists() and any(out.iterdir()) and not args.force:
        print(f"error: {out} already exists and is not empty (use --force)", file=sys.stderr)
        return 3
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    candidate = parse_facts(facts)
    slug = module_constant(_text(repo, ref, "module4/upload_names.py"), "CANDIDATE_SLUG")
    if slug:
        candidate["upload_slug"] = slug
    chains = answer_chains(_text(repo, ref, "module4/answer_book.py"))
    if chains:
        candidate["answer_chains"] = chains
    # The never-apply list was hard-coded in policy/employer_blocklist.py.
    blocklist = _text(repo, ref, "policy/employer_blocklist.py")
    for key, const in (("blocked_employers", "BLOCKED_EMPLOYERS"), ("blocked_domains", "BLOCKED_DOMAINS")):
        value = module_constant(blocklist, const) if blocklist else None
        if value:
            candidate[key] = [str(x) for x in value]

    written: list[str] = []

    def write(name: str, data, mode: int = 0o600, binary: bool = False) -> None:
        p = out / name
        if binary:
            p.write_bytes(data)
        else:
            p.write_text(data, encoding="utf-8")
        os.chmod(p, mode)
        written.append(name)

    write("candidate.json", json.dumps(candidate, indent=2, ensure_ascii=False) + "\n")
    # Exactly the text the agent saw before, minus the leading/trailing newline
    # the old triple-quoted string carried (prompts.py adds those back).
    write("candidate_facts.md", facts.strip("\n") + "\n")

    password = module_constant(_text(repo, ref, "module4/password_reset.py"), "STANDARD_PASSWORD")
    if password:
        write("secrets.json", json.dumps({"account_password": password}, indent=2) + "\n")

    for name, path, binary in (
        ("profile.md", "module3/context/profile.md", False),
        ("resume.tex", "module3/templates/resume.tex", False),
        ("resume.pdf", "module3/templates/resume.pdf", True),
    ):
        data = git_show(repo, ref, path, binary=binary)
        if data is not None:
            write(name, data, binary=binary)

    print(f"wrote {out}:")
    for name in written:
        print(f"  {name}")
    print("candidate.json keys: " + ", ".join(sorted(candidate)))
    missing = {"candidate.json", "candidate_facts.md", "secrets.json", "profile.md", "resume.tex", "resume.pdf"} - set(written)
    if missing:
        print("not found in the old revision: " + ", ".join(sorted(missing)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
