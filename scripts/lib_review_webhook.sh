#!/bin/bash
# Post-run review webhook (GHO-44), sourced by scripts/daily_pipeline.sh.
#
# notify_review <overall> POSTs one signed JSON object to the Paperclip routine
# that opens the "jobo run review" task:
#   {"run_date":"YYYY-MM-DD","slot":"HH:MM","run_ts":"<verdict ts>",
#    "overall":"<overall>","log":"<abs log path>"}
#   X-Paperclip-Timestamp: <unix seconds>
#   X-Paperclip-Signature: sha256=<hex HMAC-SHA256(secret, "<ts>.<raw body>")>
# The URL and secret come from exactly two keys in $REPO/.env
# (JOBO_REVIEW_WEBHOOK_URL, JOBO_REVIEW_WEBHOOK_SECRET); the file is never
# sourced and neither value is ever logged. Either key missing = skip.
#
# A webhook failure must never change the run's exit code or verdict, so this
# always returns 0 and logs exactly one line.
#
# Expects from the caller: REPO, LOG, STAMP (YYYYmmdd_HHMM), VERDICT_TS and
# a log() function.

# review_env_value <key> <file>: the last KEY=value line, quotes stripped.
review_env_value() {
  local key="$1" file="$2" line value
  [ -f "$file" ] || return 0
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$file" 2>/dev/null | tail -n 1)
  [ -n "$line" ] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  # Trim surrounding whitespace, then one layer of matching quotes.
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "$value"
}

# review_json_escape <string>: backslash and double-quote escaped for JSON.
review_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

# review_sign <secret> <ts> <body file>: hex HMAC-SHA256 over "<ts>.<body>".
# The secret goes through the environment, never argv (visible in ps).
review_sign() {
  REVIEW_HMAC_SECRET="$1" python3 -c '
import hashlib, hmac, os, sys
body = open(sys.argv[2], "rb").read()
msg = sys.argv[1].encode() + b"." + body
print(hmac.new(os.environ["REVIEW_HMAC_SECRET"].encode(), msg, hashlib.sha256).hexdigest())
' "$2" "$3"
}

notify_review() {
  local overall="$1"
  local env_file="$REPO/.env"
  local url secret
  url=$(review_env_value JOBO_REVIEW_WEBHOOK_URL "$env_file")
  secret=$(review_env_value JOBO_REVIEW_WEBHOOK_SECRET "$env_file")
  if [ -z "$url" ] || [ -z "$secret" ]; then
    log "review webhook: skipped (JOBO_REVIEW_WEBHOOK_URL/SECRET not set in .env)"
    return 0
  fi

  local run_date="${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2}"
  local slot="${STAMP:9:2}:${STAMP:11:2}"
  local body_file ts sig code rc
  body_file=$(mktemp) || { log "review webhook: FAILED (mktemp) - verdict unchanged"; return 0; }
  printf '{"run_date":"%s","slot":"%s","run_ts":"%s","overall":"%s","log":"%s"}' \
    "$run_date" "$slot" "$(review_json_escape "${VERDICT_TS:-}")" \
    "$(review_json_escape "$overall")" "$(review_json_escape "$LOG")" > "$body_file"
  ts=$(date +%s)
  sig=$(review_sign "$secret" "$ts" "$body_file" 2>/dev/null)
  if [ -z "$sig" ]; then
    rm -f "$body_file"
    log "review webhook: FAILED (could not compute the signature) - verdict unchanged"
    return 0
  fi
  # --data-binary sends the file's bytes untouched: exactly what was signed.
  code=$(curl -sS -o /dev/null -w '%{http_code}' --fail --max-time 15 --retry 2 --retry-delay 2 \
    -H "Content-Type: application/json" \
    -H "X-Paperclip-Timestamp: $ts" \
    -H "X-Paperclip-Signature: sha256=$sig" \
    --data-binary "@$body_file" "$url" 2>/dev/null)
  rc=$?
  rm -f "$body_file"
  if [ "$rc" -eq 0 ]; then
    log "review webhook: sent (overall=$overall, HTTP $code)"
  else
    log "review webhook: FAILED (curl exit $rc, HTTP ${code:-none}) - verdict unchanged"
  fi
  return 0
}
