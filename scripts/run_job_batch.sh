#!/bin/bash
# Run a hand-picked set of job ids through the apply stage, one per line.
#
# Why not `jobo apply` with a cap: the queue order is not the order we want to
# TEST. After shipping a fix, the jobs worth re-running are the ones whose
# stored failure matches the bug that was fixed - a random slice of the queue
# mostly re-measures dead listings and account walls, which prove nothing
# about the change.
#
# Each id runs as its own `jobo apply --job-id`, so one job crashing cannot
# take the rest of the batch with it, and the per-job boundaries in the log
# stay obvious.
#
# Usage: scripts/run_job_batch.sh <log> <id> [id ...]

set -u
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1

LOG="${1:?usage: run_job_batch.sh <log> <id> [id ...]}"
shift
IDS=("$@")
[ ${#IDS[@]} -gt 0 ] || { echo "no job ids given"; exit 1; }

{
  echo "=== batch starting $(date '+%F %T') ==="
  echo "=== jobs: ${IDS[*]} ==="
} >> "$LOG"

for id in "${IDS[@]}"; do
  {
    echo ""
    echo "=================================================================="
    echo "=== BATCH JOB $id starting $(date '+%H:%M:%S') ==="
    echo "=================================================================="
  } >> "$LOG"
  uv run jobo apply --job-id "$id" >> "$LOG" 2>&1
  rc=$?
  echo "=== BATCH JOB $id finished rc=$rc $(date '+%H:%M:%S') ===" >> "$LOG"
done

echo "=== batch complete $(date '+%F %T') ===" >> "$LOG"
