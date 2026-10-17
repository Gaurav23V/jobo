#!/usr/bin/env bash
# Drives the REAL scripts/collect_sources.sh offline (GHO-41): temp repo and
# logs, a stub `uv`, a temp lock. Checks the non-overlap lock, the env switch,
# the verdict line, and that it never takes the main pipeline's lock.
set -u
REAL_REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/scripts" "$TMP/bin" "$TMP/home"
cp "$REAL_REPO/scripts/collect_sources.sh" "$TMP/repo/scripts/"

cat > "$TMP/bin/uv" <<'EOF'
#!/bin/bash
# `uv run jobo <command> ...`
shift 2
echo "$1" >> "$STAGES_FILE"
[ -n "${STUB_SLEEP:-}" ] && sleep "$STUB_SLEEP"
[ -n "${STUB_FAIL:-}" ] && exit 1
exit 0
EOF
chmod +x "$TMP/bin/uv"

fail=0
check() {
  if [ "$1" = "yes" ]; then echo "  PASS  $2"; else echo "  FAIL  $2"; fail=$((fail+1)); fi
}
run() {
  env -i HOME="$TMP/home" PATH="$TMP/bin:/usr/bin:/bin" \
    JOBO_COLLECT_LOCK="$TMP/collect.lock" STAGES_FILE="$TMP/stages" "$@" \
    bash "$TMP/repo/scripts/collect_sources.sh"
}
last() { tail -1 "$TMP/repo/data/logs/collect_verdicts.jsonl"; }
reset() { rm -rf "$TMP/repo/data" "$TMP/stages"; : > "$TMP/stages"; }

echo "1. Normal run: collect-jobfound runs once, verdict OK"
reset; run; rc=$?
check "$([ "$rc" -eq 0 ] && [ "$(cat "$TMP/stages")" = "collect-jobfound" ] && echo yes || echo no)" "exit 0, ran collect-jobfound (rc=$rc)"
check "$(last | grep -q '"overall":"OK","jobfound":"OK"' && echo yes || echo no)" "verdict OK"

echo "2. Two runs at once: the second skips (SKIPPED_OVERLAP), no overlap"
reset
run STUB_SLEEP=3 & first=$!
sleep 1
run; rc2=$?
wait "$first"; rc1=$?
check "$([ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ] && echo yes || echo no)" "both exit 0 (rc1=$rc1 rc2=$rc2)"
check "$([ "$(grep -c . "$TMP/stages")" = 1 ] && echo yes || echo no)" "collect-jobfound ran once"
check "$(grep -q SKIPPED_OVERLAP "$TMP/repo/data/logs/collect_verdicts.jsonl" && echo yes || echo no)" "second run recorded SKIPPED_OVERLAP"

echo "3. JOBO_JOBFOUND_ENABLED=0: nothing runs"
reset; run JOBO_JOBFOUND_ENABLED=0; rc=$?
check "$([ "$rc" -eq 0 ] && [ ! -s "$TMP/stages" ] && echo yes || echo no)" "exit 0, nothing ran"
check "$(last | grep -q '"jobfound":"DISABLED"' && echo yes || echo no)" "verdict jobfound DISABLED"

echo "4. Collector failure: exit 1, verdict FAILED"
reset; run STUB_FAIL=1; rc=$?
check "$([ "$rc" -eq 1 ] && last | grep -q '"overall":"FAILED"' && echo yes || echo no)" "exit 1, verdict FAILED (rc=$rc)"

echo "5. Never touches the main pipeline lock"
check "$(grep -q 'jobo_pipeline.lock' "$REAL_REPO/scripts/collect_sources.sh" && echo no || echo yes)" "no reference to /tmp/jobo_pipeline.lock"

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "$fail CHECK(S) FAILED"
exit 1
