#!/usr/bin/env bash
# Drives the REAL scripts/daily_pipeline.sh through its weekly budget gates
# (GHO-3) without touching real jobs, the real lock, the live logs or the
# network:
#   - the script is copied into a temp "repo", so data/logs is a temp dir;
#   - `uv` is a stub: pipeline stages only record that they ran, and
#     `uv run jobo budget-gate` runs the real main.py CLI with the usage read
#     replaced by a scripted value (FAKE_UTILS: one value per gate call,
#     "none" = unreadable, "crash" = the gate command itself fails).
set -u
REAL_REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REAL_REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/scripts" "$TMP/bin" "$TMP/home"
cp "$REAL_REPO/scripts/daily_pipeline.sh" "$REAL_REPO/scripts/lib_review_webhook.sh" "$TMP/repo/scripts/"
# The temp repo has no .env, so the post-run review webhook (GHO-44) can never
# reach the real routine from here; case 11 points it at a dead local port.
rm -f "$TMP/repo/.env"
# The directsdk plugin-patch preflight (GHO-10) is not under test here.
printf '#!/bin/bash\necho "directsdk patch: present"\n' > "$TMP/repo/scripts/ensure_directsdk_patch.sh"

cat > "$TMP/gate_stub.py" <<'EOF'
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["JOBO_REAL_REPO"])
counter = Path(os.environ["FAKE_COUNTER"])
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
seq = os.environ["FAKE_UTILS"].split(",")
value = seq[min(n, len(seq) - 1)]
if value == "crash":
    sys.exit(1)
from module4 import budget_guard
budget_guard.read_access_token = lambda host_home=None: "fake-token"
def fake_fetch(token, timeout=15.0):
    if value == "none":
        return None, "usage endpoint unreadable: URLError: fake outage"
    return {
        "seven_day": {"utilization": float(value) * 100, "resets_at": "2026-10-15T11:00:00+00:00"},
        "five_hour": {"utilization": float(os.environ.get("FAKE_SESSION", "22")), "resets_at": "2026-10-15T16:29:59+00:00"},
    }, None
budget_guard.fetch_usage = fake_fetch
import main
sys.argv = ["jobo", *sys.argv[1:]]
main.cli()
EOF

cat > "$TMP/bin/uv" <<'EOF'
#!/bin/bash
# `uv run jobo <command> ...`
shift 2
if [ "$1" = "budget-gate" ]; then
  exec "$STUB_PY" -B "$STUB_DIR/gate_stub.py" "$@"
fi
echo "$1 override=${JOBO_BUDGET_OVERRIDE:-}" >> "$STAGES_FILE"
# FAKE_STAGE_OUT_<command> is printed (e.g. a Gmail auth error); a command in
# FAKE_FAIL_STAGES (space separated) exits 1.
var="FAKE_STAGE_OUT_${1//-/_}"
[ -n "${!var:-}" ] && echo "${!var}"
case " ${FAKE_FAIL_STAGES:-} " in *" $1 "*) echo "stub $1 failed"; exit 1 ;; esac
echo "stub $1 ok"
EOF
chmod +x "$TMP/bin/uv"

fail=0
check() {
  if [ "$1" = "yes" ]; then echo "  PASS  $2"; else echo "  FAIL  $2"; fail=$((fail+1)); fi
}

# run_pipeline <FAKE_UTILS> [extra env assignments...] -- [script args...]
run_pipeline() {
  local utils="$1"; shift
  rm -rf "$TMP/repo/data" "$TMP/stages" "$TMP/counter"
  : > "$TMP/stages"
  local envs=() args=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  args=("$@")
  env -i HOME="$TMP/home" PATH="$TMP/bin:/usr/bin:/bin" \
    JOBO_PIPELINE_LOCK="$TMP/pipeline.lock" JOBO_REAL_REPO="$REAL_REPO" \
    STUB_PY="$PY" STUB_DIR="$TMP" STAGES_FILE="$TMP/stages" \
    FAKE_UTILS="$utils" FAKE_COUNTER="$TMP/counter" \
    "${envs[@]+"${envs[@]}"}" \
    bash "$TMP/repo/scripts/daily_pipeline.sh" "${args[@]+"${args[@]}"}" > "$TMP/out" 2>&1
  RC=$?
  VERDICT="$TMP/repo/data/logs/pipeline_verdicts.jsonl"
  RUNLOG=$(ls "$TMP"/repo/data/logs/run_*.log 2>/dev/null | head -1)
}

stages() { cut -d' ' -f1 "$TMP/stages" | tr '\n' ' ' | sed 's/ $//'; }
# verdict_field <python expression over v> - reads the single verdict line
verdict() { "$PY" -c "import json,sys; v=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print($1)" "$VERDICT"; }
loggrep() { grep -q -- "$1" "$RUNLOG" && echo yes || echo no; }

echo "1. Skip path: 80% used at start -> no stage runs, SKIPPED_BUDGET, exit 0"
run_pipeline "0.80"
check "$([ "$RC" -eq 0 ] && echo yes || echo no)" "exit 0 (rc=$RC)"
check "$([ -z "$(stages)" ] && echo yes || echo no)" "no stage ran (ran: '$(stages)')"
check "$([ "$(verdict 'v["overall"]')" = SKIPPED_BUDGET ] && echo yes || echo no)" "verdict overall SKIPPED_BUDGET"
check "$([ "$(verdict 'v["budget"]["utilization"]')" = 0.8 ] && echo yes || echo no)" "verdict records utilization 0.8"
check "$([ "$(verdict 'v["budget"]["resets_at"]')" = 2026-10-15T11:00:00+00:00 ] && echo yes || echo no)" "verdict records the window reset time"
check "$(loggrep 'BUDGET GATE (pipeline): weekly Claude usage 80% >= 50% threshold')" "run log has the gate line"
check "$([ "$(loggrep 'review webhook')" = no ] && echo yes || echo no)" "no review webhook on SKIPPED_BUDGET"

echo
echo "2. Proceed path: 40% -> every stage runs, verdict OK with the gate result"
run_pipeline "0.40"
check "$([ "$RC" -eq 0 ] && echo yes || echo no)" "exit 0 (rc=$RC)"
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "all six stages ran (ran: '$(stages)')"
check "$([ "$(verdict 'v["overall"]')" = OK ] && echo yes || echo no)" "verdict overall OK"
check "$([ "$(verdict 'v["budget"]["decision"]')" = PROCEED ] && echo yes || echo no)" "verdict carries the last gate decision (PROCEED)"
check "$([ "$(cat "$TMP/counter")" = 2 ] && echo yes || echo no)" "gate checked twice (start + before triage)"
check "$(loggrep '5h session usage 22% (resets 2026-10-15T16:29:59+00:00)')" "run log has the 5h session usage (GHO-22)"
check "$(loggrep 'review webhook: skipped (JOBO_REVIEW_WEBHOOK_URL/SECRET not set in .env)')" "no .env -> review webhook skipped, one log line"
check "$([ "$(verdict 'v["budget"]["session_utilization"]')" = 0.22 ] && echo yes || echo no)" "verdict records the 5h session utilization"

echo
echo "3. Override flag: 90% + --budget-override -> runs, and apply gets the override"
run_pipeline "0.90" -- --budget-override
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "all six stages ran (ran: '$(stages)')"
check "$(grep -q '^apply override=1$' "$TMP/stages" && echo yes || echo no)" "apply stage sees JOBO_BUDGET_OVERRIDE=1 (between-jobs stop off)"
check "$([ "$(verdict 'v["budget"]["decision"]')" = OVERRIDE ] && echo yes || echo no)" "verdict decision OVERRIDE"
check "$(loggrep 'JOBO_BUDGET_OVERRIDE is set')" "run log says the override was used"

echo
echo "4. Override env: 90% + JOBO_BUDGET_OVERRIDE=1 -> runs"
run_pipeline "0.90" JOBO_BUDGET_OVERRIDE=1
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "all six stages ran (ran: '$(stages)')"

echo
echo "5. Crossing mid-run: 40% at start, 60% before triage -> stops before triage"
run_pipeline "0.40,0.60"
check "$([ "$RC" -eq 0 ] && echo yes || echo no)" "exit 0 (rc=$RC)"
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter" ] && echo yes || echo no)" "only collector+jobfound+enrich+prefilter ran (ran: '$(stages)')"
check "$([ "$(verdict 'v["overall"]')" = STOPPED_BUDGET ] && echo yes || echo no)" "verdict overall STOPPED_BUDGET"
check "$([ "$(verdict '",".join(s["stage"]+":"+s["verdict"] for s in v["stages"])')" = "collector:OK,jobfound:OK,enrich:OK,prefilter:OK,triage:SKIPPED_BUDGET,apply:SKIPPED_BUDGET" ] && echo yes || echo no)" "stage verdicts show triage/apply skipped for budget"
check "$([ "$(verdict 'v["budget"]["gate"]')" = triage ] && echo yes || echo no)" "verdict records the triage gate"
check "$(loggrep 'review webhook: skipped')" "STOPPED_BUDGET reaches the review webhook"

echo
echo "6. Threshold override: JOBO_BUDGET_THRESHOLD=0.5 at 56% -> skip"
run_pipeline "0.56" JOBO_BUDGET_THRESHOLD=0.5
check "$([ -z "$(stages)" ] && [ "$(verdict 'v["overall"]')" = SKIPPED_BUDGET ] && echo yes || echo no)" "skipped under a forced threshold"

echo
echo "7. Unreadable usage -> proceeds as before, logged clearly"
run_pipeline "none"
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "all six stages ran (ran: '$(stages)')"
check "$(loggrep 'weekly Claude usage UNREADABLE (usage endpoint unreadable')" "run log says UNREADABLE and why"
check "$([ "$(verdict 'v["budget"]["decision"]')" = PROCEED_UNREADABLE ] && echo yes || echo no)" "verdict decision PROCEED_UNREADABLE"

echo
echo "8. The gate command itself crashing -> proceeds, logged, verdict stays valid JSON"
run_pipeline "crash"
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "all six stages ran (ran: '$(stages)')"
check "$(loggrep 'the gate command itself failed (exit 1)')" "run log says the gate failed"
check "$([ "$(verdict 'v["budget"]["decision"]')" = GATE_ERROR ] && echo yes || echo no)" "verdict decision GATE_ERROR"

echo
echo "10. 5h session at 31% (stop at 30%) -> no stage runs, even with the override"
run_pipeline "0.13" FAKE_SESSION=31 -- --budget-override
check "$([ "$RC" -eq 0 ] && [ -z "$(stages)" ] && echo yes || echo no)" "exit 0, no stage ran (rc=$RC, ran: '$(stages)')"
check "$([ "$(verdict 'v["overall"]')" = SKIPPED_BUDGET ] && echo yes || echo no)" "verdict overall SKIPPED_BUDGET"
check "$([ "$(verdict 'v["budget"]["decision"]')" = SKIP_SESSION ] && echo yes || echo no)" "verdict decision SKIP_SESSION"
check "$(loggrep '5h session usage 31% >= 30% session line')" "run log names the session line"

echo
echo "11. Review webhook failing (dead local port) never changes the exit code or verdict"
printf 'JOBO_REVIEW_WEBHOOK_URL=http://127.0.0.1:1/hook\nJOBO_REVIEW_WEBHOOK_SECRET=test-only\n' > "$TMP/repo/.env"
run_pipeline "0.40"
rm -f "$TMP/repo/.env"
check "$([ "$RC" -eq 0 ] && [ "$(verdict 'v["overall"]')" = OK ] && echo yes || echo no)" "exit 0 and verdict OK (rc=$RC)"
check "$(loggrep 'review webhook: FAILED (curl exit 7')" "the failure is logged once"
check "$([ "$(grep -c 'review webhook' "$RUNLOG")" = 1 ] && echo yes || echo no)" "exactly one webhook log line"

echo
echo "12. SKIPPED_OVERLAP (lock held) never fires the review webhook, even with a .env"
printf 'JOBO_REVIEW_WEBHOOK_URL=http://127.0.0.1:1/hook\nJOBO_REVIEW_WEBHOOK_SECRET=test-only\n' > "$TMP/repo/.env"
exec 8>"$TMP/pipeline.lock"
flock 8
run_pipeline "0.40"
exec 8>&-
rm -f "$TMP/repo/.env"
check "$([ "$(verdict 'v["overall"]')" = SKIPPED_OVERLAP ] && [ -z "$(stages)" ] && echo yes || echo no)" "verdict SKIPPED_OVERLAP, no stage ran"
check "$([ "$(loggrep 'review webhook')" = no ] && echo yes || echo no)" "no review webhook on SKIPPED_OVERLAP"

echo
echo "9. Unknown script argument is refused before anything runs"
run_pipeline "0.10" -- --bogus
check "$([ "$RC" -eq 64 ] && [ -z "$(stages)" ] && echo yes || echo no)" "exit 64, nothing ran (rc=$RC)"

echo
echo "11. jobfound stage fails -> the run goes on, its verdict says FAILED (GHO-41)"
run_pipeline "0.10" FAKE_FAIL_STAGES=collect-jobfound
check "$([ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "every stage still ran (ran: '$(stages)')"
check "$([ "$RC" -eq 1 ] && [ "$(verdict 'v["overall"]')" = FAILED ] && echo yes || echo no)" "exit 1, overall FAILED (rc=$RC)"
check "$([ "$(verdict '",".join(s["stage"]+":"+s["verdict"] for s in v["stages"])')" = "collector:OK,jobfound:FAILED,enrich:OK,prefilter:OK,triage:OK,directsdk_patch:OK,apply:OK" ] && echo yes || echo no)" "stage verdicts: jobfound:FAILED, the rest OK"

echo
echo "12. JOBO_JOBFOUND_ENABLED=0 -> jobfound stage off, verdict DISABLED"
run_pipeline "0.10" JOBO_JOBFOUND_ENABLED=0
check "$([ "$(stages)" = "collector enrich prefilter triage apply" ] && echo yes || echo no)" "jobfound did not run (ran: '$(stages)')"
check "$([ "$RC" -eq 0 ] && [ "$(verdict 'v["overall"]')" = OK ] && echo yes || echo no)" "exit 0, overall OK (rc=$RC)"
check "$([ "$(verdict '[s["verdict"] for s in v["stages"] if s["stage"]=="jobfound"][0]')" = DISABLED ] && echo yes || echo no)" "jobfound verdict DISABLED"

echo
echo "13. Gmail auth expired -> still aborts right after the email collector"
run_pipeline "0.10" "FAKE_STAGE_OUT_collector=HttpError 401 invalid_grant"
check "$([ "$RC" -eq 2 ] && [ "$(stages)" = "collector" ] && echo yes || echo no)" "exit 2, only the collector ran (rc=$RC, ran: '$(stages)')"
check "$([ "$(verdict 'v["overall"]')" = FAILED_GMAIL_AUTH ] && echo yes || echo no)" "verdict FAILED_GMAIL_AUTH"

echo
echo "14. A jobfound error mentioning 401 does not trip the Gmail-auth abort"
run_pipeline "0.10" "FAKE_STAGE_OUT_collect_jobfound=HttpError 401 from somewhere"
check "$([ "$RC" -eq 0 ] && [ "$(stages)" = "collector collect-jobfound enrich prefilter triage apply" ] && echo yes || echo no)" "exit 0, every stage ran (rc=$RC)"

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "$fail CHECK(S) FAILED"
sed -n '1,40p' "$TMP/out"
exit 1
