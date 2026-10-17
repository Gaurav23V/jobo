#!/usr/bin/env bash
# Launch daily_pipeline.sh fully detached from the calling session.
#
# Why: a pipeline started as a plain background job dies with its parent. The
# 2026-09-05 01:13 run was killed ~30 minutes in (mid-apply, 0 submitted) when
# the launching session went away. setsid + nohup puts the run in its own
# session so it survives.
#
# The pipeline's own flock still guards against two concurrent runs.
set -u
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1

if pgrep -f 'scripts/daily_pipeline.sh' > /dev/null; then
  echo "A pipeline is already running:"
  pgrep -af 'scripts/daily_pipeline.sh'
  exit 1
fi

OUT=/tmp/jobo_pipeline_launch.out
# macOS has no setsid(1); nohup + disown still detaches the run there.
SETSID=$(command -v setsid || true)
$SETSID nohup ./scripts/daily_pipeline.sh > "$OUT" 2>&1 < /dev/null &
disown 2>/dev/null || true

sleep 5
PID=$(pgrep -f 'scripts/daily_pipeline.sh' | head -1)
if [ -z "$PID" ]; then
  echo "FAILED to start. Output:"
  cat "$OUT"
  exit 1
fi
echo "pipeline running, pid=$PID (detached; survives this session)"
echo "log: $(ls -t data/logs/run_*.log | head -1)"
echo "launch output: $OUT"
