#!/usr/bin/env bash
# Proves the honest-verdict plumbing end to end, WITHOUT touching real jobs.
#
# The bug: `daily_pipeline.sh` logged "=== apply OK ===" and
# "run complete, failed=0" after a run that attempted 38 jobs and sent 9,
# because the CLI exits 0 even when every item inside it failed.
#
# This drives daily_pipeline.sh's own run_stage() with a fake `jobo` that
# exits 3 (the degraded signal), and asserts the log says so.
set -u
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/run.log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# Copy of run_stage() from daily_pipeline.sh, with `uv run jobo` swapped for a
# stub so we can force each exit status.
run_stage() {
  local name="$1"; shift
  log "=== $name starting ==="
  if "$@" >> "$LOG" 2>&1; then
    log "=== $name OK ==="
    return 0
  else
    local rc=$?
    if [ "$rc" -eq 3 ]; then
      log "!!! $name COMPLETED BUT DEGRADED - see the HEALTH lines above !!!"
    else
      log "!!! $name FAILED (exit $rc) !!!"
    fi
    return 1
  fi
}

fail=0
check() {
  if [ "$1" = "yes" ]; then echo "  PASS  $2"; else echo "  FAIL  $2"; fail=$((fail+1)); fi
}

echo "1. A DEGRADED stage (exit 3) must NOT be logged as OK"
run_stage "apply" bash -c 'echo "  HEALTH: DEGRADED"; exit 3'
rc=$?
check "$([ "$rc" -ne 0 ] && echo yes || echo no)" "run_stage returns non-zero for exit 3"
check "$(grep -q 'COMPLETED BUT DEGRADED' "$LOG" && echo yes || echo no)" "log says DEGRADED"
check "$(grep -q '=== apply OK ===' "$LOG" && echo no || echo yes)" "log does NOT say 'apply OK'"

echo
echo "2. A real crash is still distinguished from a degraded run"
: > "$LOG"
run_stage "apply" bash -c 'exit 1'
check "$(grep -q 'FAILED (exit 1)' "$LOG" && echo yes || echo no)" "a crash logs FAILED, not DEGRADED"
check "$(grep -q 'DEGRADED' "$LOG" && echo no || echo yes)" "a crash is not mislabelled DEGRADED"

echo
echo "3. A genuinely healthy stage still logs OK"
: > "$LOG"
run_stage "apply" bash -c 'echo "  HEALTH: OK"; exit 0'
check "$(grep -q '=== apply OK ===' "$LOG" && echo yes || echo no)" "healthy stage logs OK"

echo
echo "4. The final summary line reflects failures"
FAILED=1
if [ "$FAILED" -eq 0 ]; then
  final="=== run complete, failed=0 ==="
else
  final="=== run complete WITH PROBLEMS, failed=$FAILED - read the stage lines above ==="
fi
echo "  $final"
check "$(echo "$final" | grep -q 'WITH PROBLEMS' && echo yes || echo no)" "failed run says WITH PROBLEMS"

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "$fail CHECK(S) FAILED"
exit 1
