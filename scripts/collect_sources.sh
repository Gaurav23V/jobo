#!/bin/bash
# Collector-only run for the web sources (today: jobfound.org), between the
# 10:00/22:00 pipeline runs, so no job falls out of jobfound's feed window
# before it is collected. See docs/sources.md for the schedule and cron line.
#
# Collect only: no enrich, triage or apply, and no Claude usage. jobfound rows
# arrive already enriched, filtered and deduplicated; the next daily_pipeline.sh
# run prefilters, triages and applies them.
#
# Safe next to the main pipeline:
#   * its own lock (never the daily pipeline's lock), so two
#     collect runs never overlap and a running pipeline is never blocked;
#   * the DB engine waits up to 30 s for a lock (db/database.py) and the insert
#     path commits per row (sources/store.py), so writes stay short.
#
# JOBO_JOBFOUND_ENABLED=0 turns the jobfound collector off (same switch as the
# pipeline stage). JOBO_COLLECT_LOCK overrides the lock path (tests only).

set -u
OS=$(uname -s)
export PATH="$HOME/.local/bin:$PATH"
if [ "$OS" = "Darwin" ]; then
  export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
fi

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/collect_sources.log"
VERDICT_LOG="$LOG_DIR/collect_verdicts.jsonl"
cd "$REPO" || { echo "repo missing" >&2; exit 1; }

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
verdict() {
  printf '{"ts":"%s","overall":"%s","jobfound":"%s"}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "${2:-}" >> "$VERDICT_LOG"
}

LOCK="${JOBO_COLLECT_LOCK:-/tmp/jobo_collect_sources.lock}"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    log "Another collect_sources run holds $LOCK. Skipping."
    verdict "SKIPPED_OVERLAP"
    exit 0
  fi
else
  LOCKDIR="$LOCK.d"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    OLD=$(cat "$LOCKDIR/pid" 2>/dev/null || true)
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
      log "Another collect_sources run (pid $OLD) holds $LOCKDIR. Skipping."
      verdict "SKIPPED_OVERLAP"
      exit 0
    fi
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" || { log "Could not take $LOCKDIR. Skipping."; verdict "SKIPPED_OVERLAP"; exit 0; }
  fi
  echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR"' EXIT
fi

if [ "${JOBO_JOBFOUND_ENABLED:-1}" = "0" ]; then
  log "jobfound disabled (JOBO_JOBFOUND_ENABLED=0); nothing to collect."
  verdict "OK" "DISABLED"
  exit 0
fi

log "=== jobfound starting ==="
if uv run jobo collect-jobfound --quiet >> "$LOG" 2>&1; then
  log "=== jobfound OK ==="
  verdict "OK" "OK"
  exit 0
fi
rc=$?
log "!!! jobfound FAILED (exit $rc) !!!"
verdict "FAILED" "FAILED"
exit 1
