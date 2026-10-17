#!/usr/bin/env bash
# Default regressions are disposable/offline, never the logged-in CDP browser.
# See run_isolated_regressions.py --list / --help / --self-test.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Full-suite runs also drive the real daily_pipeline.sh budget gates (offline:
# stubbed stages and usage read, temp repo/logs/lock). It spawns processes,
# which the isolated Python harness below refuses, so it runs here.
if [[ $# -eq 0 ]]; then
  bash "$ROOT/scripts/test_pipeline_budget_gate.sh"
fi
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -B "$ROOT/scripts/run_isolated_regressions.py" "$@"
fi
# Do not install/sync dependencies as a side effect of running regressions.
exec uv run --no-sync python -B "$ROOT/scripts/run_isolated_regressions.py" "$@"
