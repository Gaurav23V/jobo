#!/bin/bash
# One-shot read-only probe of the in-flight apply run.
# Used for periodic-update polling; safe to run at any time.
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1
LOG=$(ls -t data/logs/run_*.log | head -1)

SENT=$(grep -c "SUBMITTED (real application sent)" "$LOG")
DONE=$(grep -c "Applying (attempt" "$LOG")
QUEUED=$(grep -oE "Module 4: [0-9]+ jobs queued" "$LOG" | sed -E 's/.*: ([0-9]+) jobs.*/\1/' | head -1)

echo "=== $(date '+%H:%M') | $(basename "$LOG") ==="
echo "processed=$DONE/${QUEUED:-?}  APPLICATIONS_SENT=$SENT"
printf 'verdicts: '
grep -oE "Agent verdict: [a-z_]+" "$LOG" | sed 's/Agent verdict: //' | sort | uniq -c | tr '\n' ' '
echo
printf 'uploads ok/fail: %s/%s  ' \
  "$(grep -c 'Upload SUCCEEDED' "$LOG")" "$(grep -c 'Upload failed' "$LOG")"
printf 'infra-recoveries: %s  ' "$(grep -c 'Browser relaunched successfully' "$LOG")"
printf 'otp-codes: %s  ' "$(grep -c 'Retrieved emailed code' "$LOG")"
printf 'preflight-skips: %s\n' "$(grep -c 'Preflight: listing is closed' "$LOG")"

if pgrep -f daily_pipeline.sh >/dev/null; then
  echo "pipeline: RUNNING"
else
  echo "pipeline: STOPPED"
fi

if curl -s -m 3 http://127.0.0.1:9333/json/version >/dev/null 2>&1; then
  echo "browser: alive"
else
  echo "browser: DEAD"
fi

LAST=$(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG")
NOW=$(date +%s)
AGE=$((NOW - LAST))
echo "log_age=${AGE}s"
if [ "$AGE" -gt 1500 ]; then
  echo "WARNING: log has been silent for ${AGE}s - possible stall"
fi
echo "last: $(tail -1 "$LOG" | cut -c1-160)"
