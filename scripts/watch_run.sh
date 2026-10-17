#!/bin/bash
# Read-only status probe for an in-flight daily_pipeline.sh run.
# Prints a compact stage/health snapshot. Never mutates pipeline state.
cd "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1

LOG=$(ls -t data/logs/run_*.log | head -1)

echo "NOW $(date '+%F %T')"
echo "LOG $LOG (written $( (stat -c %y "$LOG" 2>/dev/null || stat -f %Sm -t "%F %T" "$LOG") | cut -d. -f1))"

echo "--- stages ---"
grep -E '=== .* (starting|OK|FAILED)|run complete' "$LOG"

echo "--- health ---"
echo "verify_retries : $(grep -cE 'failed verification' "$LOG")"
echo "fallbacks      : $(grep -ciE 'falling back to default' "$LOG")"
echo "hermes_fails   : $(grep -ciE 'hermes structured call failed|JSON validation failed|free period has ended' "$LOG")"
echo "error_lines    : $(grep -cE ' - ERROR' "$LOG")"

echo "--- db ---"
uv run python -c "
import sqlite3
c=sqlite3.connect('data/jobs.db')
q=lambda s: c.execute(s).fetchone()[0]
print('triaged        :', q('SELECT COUNT(*) FROM jobs WHERE module3_fit_score IS NOT NULL'))
print('pending apply  :', q('SELECT COUNT(*) FROM jobs WHERE should_apply=1 AND applied=0'))
print('materials ready:', q('SELECT COUNT(*) FROM jobs WHERE should_apply=1 AND applied=0 AND module3_resume_pdf_path IS NOT NULL AND module3_cover_pdf_path IS NOT NULL'))
print('applied        :', q('SELECT COUNT(*) FROM jobs WHERE applied=1'))
"

echo "--- proc ---"
if pgrep -f 'daily_pipeline.sh' >/dev/null; then
  echo "pipeline: ALIVE pid $(pgrep -f daily_pipeline.sh | head -1)"
else
  echo "pipeline: GONE"
fi
pgrep -af 'uv run jobo' | grep -oE 'jobo (triage|apply|collector|enrich).*' | head -1 || echo "stage: none"

echo "--- tail ---"
tail -3 "$LOG"
