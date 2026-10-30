#!/usr/bin/env python
"""Measure how often the first tailoring call fits one page (GHO-104).

For each job id given, runs triage's materials call once (a real Claude
call), splices and compiles the result with real LaTeX into a scratch dir,
and reports whether it is one page as the model returned it and after the
no-LLM trims the runner applies before paying for a retry. Nothing is
applied, no row is written (the DB is opened read-only) and nothing lands
in the artifacts dir.

  uv run python scripts/measure_resume_first_try.py --out /tmp/fit 2864 3103 ...
  uv run python scripts/measure_resume_first_try.py --without-budget ...

--without-budget drops the "## Line budget" prompt section, to measure the
old first-try prompt on the same jobs.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.database import DB_PATH  # noqa: E402
from db.models import JobModel  # noqa: E402
from module3 import constants, context_loader, job_bundle, llm, prompts, runner  # noqa: E402
from module3.schema import MaterialsOutput  # noqa: E402


def _without_budget(system: str) -> str:
    start = system.find("\n\n## Line budget")
    if start == -1:
        return system
    end = system.find("\n\n## ", start + 4)
    return system[:start] + system[end:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("job_ids", nargs="+", type=int)
    ap.add_argument("--out", type=Path, default=None, help="scratch dir (default: a new temp dir)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--without-budget", action="store_true")
    args = ap.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="jobo-fit-"))

    engine = create_engine(f"sqlite:///file:{DB_PATH}?mode=ro&uri=true")
    session = sessionmaker(bind=engine)()
    profile_md = context_loader.load_profile_text(constants.profile_context_path())
    base_tex = constants.base_resume_tex_path().read_text(encoding="utf-8")
    system = prompts.materials_system(
        introduction_markdown=context_loader.get_introduction_markdown(profile_md),
        base_resume_latex=base_tex,
    )
    if args.without_budget:
        system = _without_budget(system)

    tasks = []
    for jid in args.job_ids:
        job = session.get(JobModel, jid)
        if job is None:
            print(f"job_id={jid}: not in the DB, skipped")
            continue
        try:
            names = [str(x) for x in json.loads(job.module3_highlighted_projects or "[]")]
        except (json.JSONDecodeError, TypeError):
            names = []
        user = prompts.materials_user_prompt(
            job_bundle=job_bundle.build_job_bundle_text(job),
            project_excerpts=context_loader.get_project_bodies(profile_md, names),
        )
        tasks.append((jid, user))
    session.close()

    def one(task):
        jid, user = task
        try:
            mats = llm.generate_structured(
                system_instruction=system, user_text=user, response_model=MaterialsOutput
            )
        except Exception as e:  # report and carry on with the other jobs
            return jid, "error", str(e)[:200]
        skills = (mats.skills_section or "").strip()
        projects = (mats.projects_section or "").strip()
        job_dir = out / str(jid)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "blocks.json").write_text(
            json.dumps({"skills": skills, "projects": projects}, ensure_ascii=False), encoding="utf-8"
        )
        if not (skills or projects):
            return jid, "untailored", ""
        _, _, raw, _ = runner._verify_resume(
            base_tex=base_tex, skills=skills, projects=projects, art_dir=job_dir / "raw"
        )
        if not raw:
            return jid, "one page", ""
        _, _, trimmed, _, _ = runner._verify_with_trims(
            base_tex=base_tex, skills=skills, projects=projects, art_dir=job_dir / "trim", job_id=jid
        )
        if not trimmed:
            return jid, "one page after code trims", "; ".join(raw)[:160]
        return jid, "needs a retry", "; ".join(trimmed)[:300]

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(one, tasks))

    counts: dict[str, int] = {}
    for jid, verdict, detail in results:
        counts[verdict] = counts.get(verdict, 0) + 1
        print(f"job_id={jid}: {verdict}" + (f" ({detail})" if detail else ""))
    tailored = sum(n for v, n in counts.items() if v not in ("untailored", "error"))
    first = counts.get("one page", 0)
    trims = counts.get("one page after code trims", 0)
    print(f"\nprompt: {'without' if args.without_budget else 'with'} line budget; blocks in {out}")
    print(f"tailored {tailored} of {len(results)} (untailored {counts.get('untailored', 0)}, "
          f"errors {counts.get('error', 0)})")
    if tailored:
        print(f"first try one page: {first}/{tailored} as returned, "
              f"{first + trims}/{tailored} with code trims; "
              f"{counts.get('needs a retry', 0)} would need a second call")
    return 0


if __name__ == "__main__":
    sys.exit(main())
