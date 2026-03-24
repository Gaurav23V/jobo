<!--
Parser rule (context_loader.py): every `## Heading` block is one project; the
heading text must match `highlighted_project_names` from Gemini. Use `#` only
for non-project sections (intro, experience). Copy this file to profile.md
(gitignored) and edit.
-->

# Introduction

Replace this file’s content with your real profile. Copy to `profile.md` (or set `JOBO_PROFILE_CONTEXT_PATH`) and fill in.

Brief intro about yourself and what you want in your next role.

# Experience

Summarize roles, years, domains—whatever Module 3 should know when judging fit.

## Project One

- **GitHub:** https://github.com/example/project-one
- **Stack:** Python, PostgreSQL
- **Description:** What you built and impact.
- **Links:** Demo, blog, etc.

## Project Two

- **GitHub:** https://github.com/example/project-two
- **Stack:** TypeScript, React
- **Description:** ...

<!--
Convention (see docs/module3_overview.md):

- Non-project sections: top-level `#` only (e.g. `# Introduction`, `# Experience`).
- Each project: `## Exact Name` — the heading text must match what the LLM returns
  in `highlighted_project_names` (exact string match after strip).

Copy this file to `profile.md` and edit; keep `profile.md` out of git if it is private.
-->
