#!/bin/bash
# Read the current state of an apply run in ONE call.
#
# Prints the live counters, the failure mix so far, and - the part that matters
# for deciding whether to intervene - whether the run is progressing, idle, or
# genuinely stuck. Pair with monitor_apply.sh, which records the same counters
# on a timer so a late check can still see the history.
#
# Usage: scripts/apply_status.sh <run-log>

set -u
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1
# shellcheck source=scripts/lib_apply_alive.sh
. scripts/lib_apply_alive.sh

LOG="${1:?usage: apply_status.sh <run-log>}"
MON="${LOG}.monitor"

[ -f "$LOG" ] || { echo "no log at $LOG"; exit 1; }

RUNNING=no
apply_alive && RUNNING=yes
AGE=$(( $(date +%s) - $(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG") ))

echo "=== $(date '+%H:%M:%S')  running=$RUNNING  log_age=${AGE}s ==="
echo "attempted=$(grep -c 'Applying (attempt' "$LOG")" \
     "SENT=$(grep -c 'Agent verdict: submitted' "$LOG")" \
     "preflight_closed=$(grep -c 'Preflight: listing is closed' "$LOG")" \
     "blocked=$(grep -c 'BLOCKED EMPLOYER' "$LOG")"
echo "uploads ok/fail=$(grep -c 'Upload SUCCEEDED' "$LOG")/$(grep -c 'Upload failed' "$LOG")" \
     "infra_failures=$(grep -c 'Infrastructure failure' "$LOG")" \
     "browser_relaunches=$(grep -c 'Browser relaunched successfully' "$LOG")" \
     "answer_substitutions=$(grep -c 'site had no such option' "$LOG")"

printf 'verdicts: '
grep -oE "Agent verdict: [a-z_]+" "$LOG" | sed 's/Agent verdict: //' | sort | uniq -c | tr '\n' ' '
echo

# Field-handoff health: a spike in repeated failures on ONE field number is the
# classic budget-eating loop, and is what we most want to catch early.
printf 'field-set failures by field: '
grep -oE "Field set failed \([0-9]/3 for #[0-9]+\)" "$LOG" \
  | grep -oE "#[0-9]+" | sort | uniq -c | sort -rn | head -5 | tr '\n' ' '
echo

echo "--- last 3 verdicts ---"
grep -oE "\[[0-9]+\] Applying.*|Agent verdict: .{0,110}" "$LOG" | tail -6

if [ "$AGE" -gt 1500 ] && [ "$RUNNING" = yes ]; then
  echo ">>> STALL SUSPECTED: ${AGE}s with no output. Check the current job."
fi

if [ -f "$MON" ]; then
  echo "--- monitor tail ---"
  tail -3 "$MON"
fi
