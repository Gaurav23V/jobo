#!/bin/bash
# Continuous recorder for an in-flight apply run.
#
# Why this exists: the agent watching a long run is asked for updates every N
# minutes and has repeatedly gone quiet for 30+ minutes instead. Polling from
# the agent side is unreliable by nature - it depends on the agent remembering.
# This records the run's state on a fixed timer regardless, so a LATE poll
# still recovers the entire history rather than a single instantaneous sample,
# and so "when exactly did it stall" is answerable after the fact.
#
# It also does the judging the agent kept having to redo by hand: it flags a
# job that has been on the same job id with no new log output for longer than
# STALL_SEC, which is the signal to go and look.
#
# Usage: scripts/monitor_apply.sh <run-log> [interval-sec]
# Writes: <run-log>.monitor  (one block per sample, newest appended)

set -u
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1
# shellcheck source=scripts/lib_apply_alive.sh
. scripts/lib_apply_alive.sh

LOG="${1:?usage: monitor_apply.sh <run-log> [interval]}"
INTERVAL="${2:-60}"
OUT="${LOG}.monitor"
# A single job legitimately takes up to ~20min on the escalated budget, so only
# treat silence well beyond that as a stall worth interrupting for.
STALL_SEC=1500

echo "monitor started $(date '+%F %T') pid=$$ log=$LOG interval=${INTERVAL}s" >> "$OUT"

while true; do
  NOW=$(date '+%H:%M:%S')

  if [ ! -f "$LOG" ]; then
    echo "[$NOW] waiting for log to appear" >> "$OUT"
    sleep "$INTERVAL"
    continue
  fi

  RUNNING=no
  apply_alive && RUNNING=yes

  SENT=$(grep -c "Agent verdict: submitted" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  ATTEMPTED=$(grep -c "Applying (attempt" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  PREFLIGHT=$(grep -c "Preflight: listing is closed" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  BLOCKED=$(grep -c "BLOCKED EMPLOYER" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  INFRA=$(grep -c "Infrastructure failure" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  RELAUNCH=$(grep -c "Browser relaunched successfully" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  UPOK=$(grep -c "Upload SUCCEEDED" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  UPBAD=$(grep -c "Upload failed" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)
  SUBST=$(grep -c "site had no such option" "$LOG" 2>/dev/null | tr -d '\n' || echo 0)

  CURJOB=$(grep -oE "^\[?[0-9]{4}-[0-9]{2}-[0-9]{2}.*\[([0-9]+)\] Applying" "$LOG" 2>/dev/null \
           | grep -oE "\[[0-9]+\] Applying" | tail -1 | grep -oE "[0-9]+" | head -1)
  AGE=$(( $(date +%s) - $(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG") ))

  BROWSER=dead
  curl -s -m 3 http://127.0.0.1:9333/json/version >/dev/null 2>&1 && BROWSER=alive

  {
    echo "[$NOW] running=$RUNNING job=${CURJOB:-?} attempted=$ATTEMPTED sent=$SENT" \
         "preflight_closed=$PREFLIGHT blocked=$BLOCKED infra=$INFRA relaunch=$RELAUNCH" \
         "uploads=$UPOK/$UPBAD substitutions=$SUBST browser=$BROWSER log_age=${AGE}s"
    if [ "$AGE" -gt "$STALL_SEC" ] && [ "$RUNNING" = yes ]; then
      echo "[$NOW] *** STALL SUSPECTED: no log output for ${AGE}s on job ${CURJOB:-?} ***"
    fi
  } >> "$OUT"

  if [ "$RUNNING" = no ] && [ "$ATTEMPTED" -gt 0 ]; then
    echo "[$NOW] run finished (apply process gone)" >> "$OUT"
    exit 0
  fi

  sleep "$INTERVAL"
done
