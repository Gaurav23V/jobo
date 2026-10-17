#!/bin/bash
# jobo daily pipeline: collector -> jobfound -> enrich -> prefilter -> triage -> apply
# Runs via cron at 10:00 and 22:00 IST. All state lives in data/jobs.db;
# every stage is idempotent so reruns never duplicate work.
#
# Claude budget gate (the user's rule): if 50% or more of the weekly Claude
# allowance, or 30% or more of the 5-hour session, is used, the run is
# skipped (checked again before triage, and
# between apply jobs). It resumes on its own once the weekly window resets.
# To force ONE run because the user explicitly asked for it:
#   bash scripts/daily_pipeline.sh --budget-override
#   (or JOBO_BUDGET_OVERRIDE=1 bash scripts/daily_pipeline.sh)
# Never put the override in cron's environment.

set -u
for arg in "$@"; do
  case "$arg" in
    --budget-override) export JOBO_BUDGET_OVERRIDE=1 ;;
    *) echo "usage: $0 [--budget-override]" >&2; exit 64 ;;
  esac
done
# Works on Linux (cron) and macOS. Paths are
# derived from $HOME and this script's location, never hardcoded.
OS=$(uname -s)
export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
if [ "$OS" = "Darwin" ]; then
  # cron/launchd on macOS start with a bare PATH; add Homebrew (pandoc,
  # chromium, pdfinfo), /usr/local/bin (ollama) and the per-user TinyTeX.
  export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/Library/TinyTeX/bin/universal-darwin:$PATH"
fi

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
STAMP=$(date +%Y%m%d_%H%M)
LOG="$LOG_DIR/run_$STAMP.log"

# GUI session env for the headed browser (LinkedIn profile is signed in there).
# X11 only: macOS opens browser windows without DISPLAY/XAUTHORITY.
if [ "$OS" = "Linux" ]; then
  XAUTH=$(ps -eo args | grep -oE '\-auth [^ ]+mutter-Xwaylandauth[^ ]+' | head -1 | awk '{print $2}')
  export DISPLAY=:0
  export XAUTHORITY="$XAUTH"
fi
export JOBO_APPLY_DELAY_SEC=30
export JOBO_APPLY_JITTER_SEC=15

cd "$REPO" || { echo "repo missing" >&2; exit 1; }

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# Per-stage verdicts for the structured record below (plain indexed array -
# works on bash 3.2/macOS too; no associative arrays).
STAGE_VERDICTS=()

# A durable, structured, one-line-per-run record - separate from the free-text
# log - so "did cron fire, and did any stage silently fail" is one `tail` away
# instead of grepping a multi-hundred-line log. Called explicitly at every
# single exit path below (overlap-skip, Gmail-auth-abort, normal completion),
# so even a slot that never ran a stage still leaves a line.
VERDICT_LOG="$LOG_DIR/pipeline_verdicts.jsonl"
write_verdict() {
  local overall="$1"
  # The latest budget gate result (JSON object), recorded as "budget".
  local budget="${2:-${BUDGET_JSON:-}}"
  local stages_json="["
  local first=1
  for entry in "${STAGE_VERDICTS[@]+"${STAGE_VERDICTS[@]}"}"; do
    local stage_name="${entry%%:*}"
    local stage_verdict="${entry#*:}"
    if [ "$first" -eq 1 ]; then first=0; else stages_json+=","; fi
    stages_json+="{\"stage\":\"$stage_name\",\"verdict\":\"$stage_verdict\"}"
  done
  stages_json+="]"
  local budget_json=""
  if [ -n "$budget" ]; then budget_json=",\"budget\":$budget"; fi
  printf '{"ts":"%s","overall":"%s","stages":%s%s,"log":"%s"}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$overall" "$stages_json" "$budget_json" "$LOG" >> "$VERDICT_LOG"
}

# Overlap guard: never run two pipelines concurrently. A long apply stage
# used to survive into the next cron slot, so 10:00/22:00 runs stacked up
# (three instances observed 2026-08-26) and fought over one browser profile.
# JOBO_PIPELINE_LOCK exists only so scripts/test_pipeline_budget_gate.sh can
# exercise this script without contending for the real lock.
LOCK="${JOBO_PIPELINE_LOCK:-/tmp/jobo_pipeline.lock}"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    log "Another pipeline instance is already running (flock $LOCK held). Skipping this slot."
    write_verdict "SKIPPED_OVERLAP"
    exit 0
  fi
else
  # macOS has no flock(1). mkdir is atomic; the pid file lets a lock left by
  # a crashed run be reclaimed instead of blocking every later slot.
  LOCKDIR="$LOCK.d"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    OLD=$(cat "$LOCKDIR/pid" 2>/dev/null || true)
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
      log "Another pipeline instance is already running (pid $OLD holds $LOCKDIR). Skipping this slot."
      write_verdict "SKIPPED_OVERLAP"
      exit 0
    fi
    log "Reclaiming stale lock $LOCKDIR (pid ${OLD:-unknown} is gone)."
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" || { log "Could not take $LOCKDIR. Skipping this slot."; write_verdict "SKIPPED_OVERLAP"; exit 0; }
  fi
  echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR"' EXIT
fi

run_stage() {
  local name="$1"; shift
  log "=== $name starting ==="
  if uv run jobo "$@" >> "$LOG" 2>&1; then
    log "=== $name OK ==="
    STAGE_VERDICTS+=("$name:OK")
    return 0
  else
    local rc=$?
    # Exit 3 is the honest-health signal: the stage RAN to completion but its
    # own outcome counters say it went badly (e.g. most jobs failed, or the
    # browser died mid-run). Distinguished from a real crash so the log says
    # which one happened - "=== apply OK ===" after 28 of 38 jobs failed is
    # the bug this exists to prevent.
    if [ "$rc" -eq 3 ]; then
      log "!!! $name COMPLETED BUT DEGRADED - see the HEALTH lines above !!!"
      STAGE_VERDICTS+=("$name:DEGRADED")
    else
      log "!!! $name FAILED (exit $rc) !!!"
      STAGE_VERDICTS+=("$name:FAILED")
    fi
    return 1
  fi
}

# Weekly Claude budget gate (module4/budget_guard.py via `jobo budget-gate`).
# Returns 1 only when usage is at/over the threshold and no override is set;
# leaves the gate's JSON in BUDGET_JSON, which write_verdict records. An unreadable
# usage read, or the gate command itself breaking, proceeds as before (apply
# still re-checks between jobs) - but says so loudly in the log.
BUDGET_JSON=""
budget_gate() {
  local stage="$1" err rc line
  err=$(mktemp)
  BUDGET_JSON=$(uv run jobo budget-gate --stage "$stage" 2>"$err")
  rc=$?
  while IFS= read -r line; do log "$line"; done < "$err"
  rm -f "$err"
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 75 ]; then
    log "BUDGET GATE ($stage): the gate command itself failed (exit $rc); weekly usage unknown - proceeding as before."
    BUDGET_JSON=""
  fi
  # Only ever embed a single JSON object in the verdict line.
  case "$BUDGET_JSON" in
    "{"*"}") ;;
    *) BUDGET_JSON="{\"gate\":\"$stage\",\"decision\":\"GATE_ERROR\",\"exit\":$rc}" ;;
  esac
  [ "$rc" -ne 75 ]
}

if ! budget_gate "pipeline"; then
  log "=== run skipped: Claude budget line reached (50% weekly or 30% of the 5h session) - no stage ran ==="
  write_verdict "SKIPPED_BUDGET"
  exit 0
fi

FAILED=0

# Stage 1: pull mail from past 7 days. Dedup: existing URLs skipped, no writes.
if ! run_stage "collector" collector --hours 168; then FAILED=1; fi

# Gmail refresh token dies ~2026-08-29 (Testing-mode OAuth). Detect auth failure
# and stop early rather than running the rest of the pipeline on stale data.
if grep -qE 'invalid_grant|Requested entity was not found|HttpError 401' "$LOG"; then
  log "Gmail auth appears expired. Run: cd $REPO && uv run python auth_manual.py"
  write_verdict "FAILED_GMAIL_AUTH"
  exit 2
fi

# Stage 1b: jobfound.org (sources/jobfound.py). Rows arrive already enriched
# from jobfound's own data, filtered and deduplicated against every source.
# A failure here is recorded but never stops the run (the email jobs still go
# through). JOBO_JOBFOUND_ENABLED=0 turns the stage off. It runs after the
# Gmail-auth check above, so its output can never trip that check.
if [ "${JOBO_JOBFOUND_ENABLED:-1}" = "0" ]; then
  log "=== jobfound disabled (JOBO_JOBFOUND_ENABLED=0) ==="
  STAGE_VERDICTS+=("jobfound:DISABLED")
elif ! run_stage "jobfound" collect-jobfound --quiet; then
  FAILED=1
fi

# Stage 2: enrich un-enriched rows only (module2_attempted flag gates it).
if ! run_stage "enrich" enrich; then FAILED=1; fi

# Stage 2b: rule filters (policy/job_filters.py) on enriched, unscored rows of
# every source. Dropped rows get prefilter_reason and are never triaged, so
# they cost no Claude. No Claude here, so it runs before the triage gate.
if ! run_stage "prefilter" prefilter --quiet; then FAILED=1; fi

# Re-check before triage: collector/enrich can take a while, and triage is the
# first stage that spends Claude (`claude -p`). Apply re-checks between jobs.
if ! budget_gate "triage"; then
  STAGE_VERDICTS+=("triage:SKIPPED_BUDGET" "apply:SKIPPED_BUDGET")
  log "=== run stopped before triage: Claude budget line reached (50% weekly or 30% of the 5h session) ==="
  write_verdict "STOPPED_BUDGET"
  exit $FAILED
fi

# Stage 3: triage rows with module2_enriched_at set that lack fit scores.
# Phase-1 skip rule makes this a no-op for already-scored jobs.
if ! run_stage "triage" triage --quiet; then FAILED=1; fi

# Hermes' Claude plugin needs a local fix or every apply agent answers with a
# session-title JSON and no verdict (GHO-10); a plugin update drops the fix.
if bash "$REPO/scripts/ensure_directsdk_patch.sh" >> "$LOG" 2>&1; then
  STAGE_VERDICTS+=("directsdk_patch:OK")
else
  log "!!! directsdk patch missing - applies may all end verdict-less, see GHO-10 !!!"
  STAGE_VERDICTS+=("directsdk_patch:FAILED")
  FAILED=1
fi

# Stage 4: apply to should_apply jobs not yet applied. Pacing ~30-45s between
# two real submissions to the same apply host (module4/runner.py); dead
# listings, failures and jobs on other hosts do not wait.
# A cheap preflight skips provably-closed LinkedIn postings without spawning
# an agent, and email-OTP walls are resolved via the NEED_OTP handoff.
# NOTE: no --quiet here (unlike collector/triage) - apply's per-job INFO logs
# ("Applying (attempt N/3)...", "Sleeping Ns...", agent verdicts) are the only
# way to see what a multi-hour run is actually doing; --quiet drops to
# WARNING-only and silently swallowed all of this in every historical log.
#
# Parallel apply (GHO-5, module4/parallel.py): one LinkedIn worker on the
# signed-in browser routes each listing and applies Easy Apply itself; the
# form workers (own Chromium + LinkedIn-free profile each, CDP 9342+) apply on
# employers' own sites. JOBO_APPLY_FORM_WORKERS=0 is the old single worker.
FORM_WORKERS="${JOBO_APPLY_FORM_WORKERS:-3}"
if ! run_stage "apply" apply --form-workers "$FORM_WORKERS"; then FAILED=1; fi

if [ "$FAILED" -eq 0 ]; then
  log "=== run complete, failed=0 ==="
  write_verdict "OK"
else
  log "=== run complete WITH PROBLEMS, failed=$FAILED - read the stage lines above ==="
  write_verdict "FAILED"
fi
exit $FAILED
