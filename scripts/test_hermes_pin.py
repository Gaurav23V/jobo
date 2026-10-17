#!/usr/bin/env python3
"""The apply agent's Hermes must run its turns in-process (GHO-13), offline.

On 15 Oct 2026 Hermes auto-updated to its unified gateway runtime: every
`hermes chat` turn then ran inside one shared gateway process, which never saw
the BROWSER_CDP_URL (or the JOBO_* plugin values) the apply agent is started
with. Every agent drove a fresh local headless browser instead of its worker's
CDP browser: Easy Apply looked signed out, uploads found no file input,
nothing submitted, and form jobs were still charged attempts.

  [1] which hermes: JOBO_HERMES_BIN, else the pinned tree, else PATH
  [2] the source tree is read through the installer's shim + launcher
  [3] gateway-only detection: a tree that swaps in GatewayAgent is refused,
      an older tree and an unreadable launcher are not
  [4] run_module4 with a gateway-only Hermes: stage does not start, no
      browser, no agent, no attempt charged, health DEGRADED; a parallel
      worker also raises the run's stop flag
  [5] run_parallel refuses before queueing or starting any worker
  [6] the pinned launcher runs the pinned tree with its own venv python

No network, no model, no subprocess. Writes only under TMPDIR.

    uv run python scripts/test_hermes_pin.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["JOBO_LINKEDIN_SESSION_CHECK"] = "0"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module4 import agent_driver, apply_queue, browser_session, budget_guard, parallel, runner  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' - ' + extra) if extra and not ok else ''}")
    if not ok:
        FAILURES.append(name)


TMP = Path(tempfile.mkdtemp(prefix="gho13_"))

# The gateway runtime's swap, verbatim from hermes_cli/cli_agent_setup_mixin.py (297552a9d6).
GATEWAY_SETUP = (
    "    def _init_agent(self):\n"
    "        from hermes_cli.gateway_agent import GatewayAgent as AIAgent  # the gateway runs the turn\n"
)
INPROCESS_SETUP = "    def _init_agent(self):\n        from run_agent import AIAgent\n"


def make_tree(name: str, setup: str) -> Path:
    root = TMP / name
    (root / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (root / "hermes_cli" / "main.py").write_text("def main():\n    return 0\n")
    (root / "hermes_cli" / "cli_agent_setup_mixin.py").write_text(setup)
    return root


def make_install(name: str, tree: Path) -> Path:
    """The installer's layout: ~/.local/bin/hermes (sh shim) -> <tree>/.hermes/bin/hermes (launcher)."""
    launcher = tree / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    # Shape of the real launcher: a python -c program in single quotes, so
    # its own quotes appear as '"'"'.
    launcher.write_text(
        "#!/bin/sh\n"
        "exec /opt/python3 -I -c 'import os, re, sys\n"
        "os.environ.pop('\"'\"'PYTHONPATH'\"'\"', None)\n"
        f"sys.path.insert(0, '\"'\"'{tree}'\"'\"')\n"
        "from hermes_cli.main import main\n"
        "sys.exit(main())' \"$@\"\n"
    )
    shim = TMP / name / "hermes"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f'#!/bin/sh\nexec {launcher} "$@"\n')
    return shim


GATEWAY_TREE = make_tree("gateway-tree", GATEWAY_SETUP)
OLD_TREE = make_tree("old-tree", INPROCESS_SETUP)
GATEWAY_BIN = make_install("gateway-bin", GATEWAY_TREE)
OLD_BIN = make_install("old-bin", OLD_TREE)


def env(**kv):
    """patch.dict on os.environ; a None value removes the key."""
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {k: v for k, v in kv.items() if v is not None}))
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
    return stack


def t1_which_hermes() -> None:
    print("[1] which hermes the agent runs")
    with env(JOBO_HERMES_BIN="/opt/custom/hermes", JOBO_HERMES_PIN_DIR=str(OLD_TREE)):
        check("JOBO_HERMES_BIN wins", agent_driver._hermes_bin() == "/opt/custom/hermes")
    with env(JOBO_HERMES_BIN=None, JOBO_HERMES_PIN_DIR=str(OLD_TREE)):
        check("an existing pin is used through scripts/hermes_pinned.sh",
              agent_driver._hermes_bin() == str(agent_driver.PINNED_HERMES))
    with env(JOBO_HERMES_BIN=None, JOBO_HERMES_PIN_DIR=str(TMP / "no-pin")), \
            patch.object(agent_driver.shutil, "which", return_value="/usr/local/bin/hermes"):
        check("no pin: hermes on PATH", agent_driver._hermes_bin() == "/usr/local/bin/hermes")


def t2_source_root() -> None:
    print("[2] source tree behind a launcher")
    check("installer shim -> launcher -> tree", agent_driver.hermes_source_root(str(GATEWAY_BIN)) == GATEWAY_TREE,
          str(agent_driver.hermes_source_root(str(GATEWAY_BIN))))
    with env(JOBO_HERMES_PIN_DIR=str(OLD_TREE)):
        check("the pinned launcher maps to the pin dir",
              agent_driver.hermes_source_root(str(agent_driver.PINNED_HERMES)) == OLD_TREE.resolve())
    check("missing file -> unknown", agent_driver.hermes_source_root(str(TMP / "nope")) is None)


def t3_gateway_detection() -> None:
    print("[3] gateway-only detection")
    check("gateway runtime tree is gateway-only", agent_driver.hermes_gateway_only(str(GATEWAY_BIN)))
    check("in-process tree is fine", not agent_driver.hermes_gateway_only(str(OLD_BIN)))
    check("unreadable launcher counts as fine", not agent_driver.hermes_gateway_only(str(TMP / "nope")))
    with env(JOBO_HERMES_PIN_DIR=str(GATEWAY_TREE)):
        check("a pin that is itself gateway-only is refused",
              agent_driver.hermes_gateway_only(str(agent_driver.PINNED_HERMES)))


def make_db(tag: str, n: int):
    engine = create_engine(f"sqlite:///{TMP / f'jobs_{tag}.db'}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    resume = TMP / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 stub")
    with Session() as s:
        for i in range(1, n + 1):
            s.add(JobModel(
                id=i, company_name=f"Employer {i}", job_title="Engineer",
                job_url=f"https://www.linkedin.com/jobs/view/{i}/", should_apply=True, applied=False,
                module3_fit_score=4, module3_resume_pdf_path=str(resume), module4_attempts=0,
            ))
        s.commit()
    return Session


def _boom(*a, **k):
    raise AssertionError("must not be reached with a gateway-only Hermes")


def t4_runner_refuses() -> None:
    print("[4] run_module4 refuses a gateway-only Hermes")
    Session = make_db("single", 3)
    budget = lambda: budget_guard.decide(None, 1.0)  # noqa: E731
    with env(JOBO_HERMES_BIN=str(GATEWAY_BIN)), \
            patch.object(browser_session, "ensure_browser_running", side_effect=_boom), \
            patch.object(agent_driver, "run_apply_agent", side_effect=_boom), Session() as s:
        try:
            result = runner.run_module4(s, budget_check=budget)
            raised = ""
        except AssertionError as e:
            result, raised = None, str(e)
    check("no browser and no agent", not raised, raised)
    if result is None:
        return
    check("nothing attempted or submitted", result.attempted == 0 and result.submitted == 0)
    check("reason recorded", "gateway" in result.hermes_unusable and any("ABORTED" in d for d in result.details))
    v = runner.health_verdict(result)
    check("health DEGRADED, reason surfaced", not v.healthy and any("gateway" in r for r in v.reasons), str(v))
    with Session() as s:
        rows = list(s.query(JobModel))
    check("no job charged an attempt", all((j.module4_attempts or 0) == 0 and not j.module4_last_status for j in rows))

    q = apply_queue.ApplyQueue("gho13_worker", path=TMP / "queue.sqlite")
    q.create_run([(1, "Employer 1")])
    w = parallel.Worker("li", parallel.ROLE_LINKEDIN, q, form_workers=1, poll_sec=0.01)
    q.register_worker("li", w.role, None, None)
    with env(JOBO_HERMES_BIN=str(GATEWAY_BIN)), \
            patch.object(browser_session, "ensure_browser_running", side_effect=_boom), Session() as s:
        runner.run_module4(s, worker=w, budget_check=budget)
    check("worker raised the run's stop flag", bool(q.stop_reason()), str(q.stop_reason()))
    check("worker claimed nothing", all(r["state"] == "pending" for r in q.rows()), str(q.counts()))

    with env(JOBO_HERMES_BIN=str(OLD_BIN)):
        r = runner.Module4Result()
        check("an in-process Hermes passes the preflight", runner.hermes_preflight(r) and not r.hermes_unusable)


def t5_parallel_refuses() -> None:
    print("[5] run_parallel refuses before starting workers")
    Session = make_db("parallel", 3)
    with env(JOBO_HERMES_BIN=str(GATEWAY_BIN)), \
            patch.object(parallel.subprocess, "Popen", side_effect=_boom), \
            patch.object(parallel.apply_queue.ApplyQueue, "create_run", side_effect=_boom), Session() as s:
        try:
            result = parallel.run_parallel(s, form_workers=3, budget_check=lambda: budget_guard.decide(None, 1.0))
            raised = ""
        except AssertionError as e:
            result, raised = None, str(e)
    check("no queue and no worker process", not raised, raised)
    check("reason recorded", result is not None and "gateway" in result.hermes_unusable)


def t6_launcher() -> None:
    print("[6] the pinned launcher")
    text = agent_driver.PINNED_HERMES.read_text()
    check("launcher is executable", os.access(agent_driver.PINNED_HERMES, os.X_OK))
    check("defaults to ~/.hermes/jobo-hermes-pin/current", "$HOME/.hermes/jobo-hermes-pin/current" in text)
    check("honours JOBO_HERMES_PIN_DIR like agent_driver", "JOBO_HERMES_PIN_DIR" in text)
    check("runs the pin's own venv python (pyvenv.cfg sits next to it)", '"$PIN/.venv/bin/python"' in text)
    check("puts the pinned tree first on sys.path", "sys.path.insert(0, os.environ.pop(\"JOBO_HERMES_PIN_ROOT\"))" in text)
    check("fails loudly without a pin", "exit 127" in text)


def main() -> int:
    for t in (t1_which_hermes, t2_source_root, t3_gateway_detection, t4_runner_refuses,
              t5_parallel_refuses, t6_launcher):
        t()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS: apply agent Hermes pin and gateway guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
