#!/usr/bin/env bash
# Default regressions are disposable/offline, never the logged-in CDP browser.
# See run_isolated_regressions.py --list / --help / --self-test.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Full-suite runs also drive the real daily_pipeline.sh budget gates (offline:
# stubbed stages and usage read, temp repo/logs/lock). It spawns processes,
# which the isolated Python harness below refuses, so it runs here.
# The merge gate's test (GHO-45) builds throwaway git repos the same way.
if [[ $# -eq 0 ]]; then
  bash "$ROOT/scripts/test_pipeline_budget_gate.sh"
  bash "$ROOT/scripts/test_collect_sources.sh"
  # The post-run review webhook (GHO-44) against a 127.0.0.1 HTTP stub.
  bash "$ROOT/scripts/test_review_webhook.sh"
  bash "$ROOT/scripts/test_premerge_check.sh"
fi
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -B "$ROOT/scripts/run_isolated_regressions.py" "$@"
fi
# Do not install/sync dependencies as a side effect of running regressions.
exec uv run --no-sync python -B "$ROOT/scripts/run_isolated_regressions.py" "$@"
