#!/usr/bin/env bash
# Drives scripts/lib_review_webhook.sh's notify_review (GHO-44) against a local
# HTTP stub on 127.0.0.1, never the real routine:
#   - the body is the agreed JSON object and the signature is
#     HMAC-SHA256(secret, "<ts>.<raw body>") over the exact bytes received;
#   - only the two webhook keys are read from .env (the file is not sourced);
#   - a missing key, a 5xx or an unreachable URL logs one line and returns 0.
set -u
REAL_REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REAL_REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3

TMP=$(mktemp -d)
STUB_PID=""
cleanup() { [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

cat > "$TMP/stub.py" <<'EOF'
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
out, status_file = sys.argv[1], sys.argv[2]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(out, "a") as f:
            f.write(json.dumps({"path": self.path, "headers": dict(self.headers),
                                "body": body.decode("latin-1")}) + "\n")
        code = int(open(status_file).read().strip() or 202)
        self.send_response(code); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
srv = HTTPServer(("127.0.0.1", 0), H)
open(out + ".port", "w").write(str(srv.server_address[1]))
srv.serve_forever()
EOF
echo 202 > "$TMP/status"
: > "$TMP/requests"
"$PY" -B "$TMP/stub.py" "$TMP/requests" "$TMP/status" &
STUB_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP/requests.port" ] && break; sleep 0.1; done
PORT=$(cat "$TMP/requests.port")

fail=0
check() {
  if [ "$1" = "yes" ]; then echo "  PASS  $2"; else echo "  FAIL  $2"; fail=$((fail+1)); fi
}

SECRET='s3cr3t-"quoted"-value'
REPO="$TMP/repo"
mkdir -p "$REPO"
LOG="$TMP/run_20261018_2200.log"
STAMP=20261018_2200
VERDICT_TS=2026-10-18T16:44:03Z
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
# shellcheck source=lib_review_webhook.sh
. "$REAL_REPO/scripts/lib_review_webhook.sh"

write_env() {
  # A line that would run a command if the file were sourced, other keys,
  # an export prefix, quotes and a CRLF ending: only the two keys are read.
  {
    echo "TOUCHED=\$(touch $TMP/pwned)"
    echo "OTHER_SECRET=do-not-read"
    echo "export JOBO_REVIEW_WEBHOOK_URL=\"$1\""
    printf "JOBO_REVIEW_WEBHOOK_SECRET='%s'\r\n" "$SECRET"
  } > "$REPO/.env"
}
nreq() { wc -l < "$TMP/requests" | tr -d ' '; }
last_log() { tail -n 1 "$LOG"; }
req() { "$PY" -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1])][-1]; print($1)" "$TMP/requests"; }

echo "1. Delivery: body, headers and signature"
write_env "http://127.0.0.1:$PORT/hook?x=1"
: > "$TMP/requests"; : > "$LOG"
before=$(date +%s)
notify_review "OK"; rc=$?
check "$([ "$rc" -eq 0 ] && echo yes || echo no)" "returns 0"
check "$([ "$(nreq)" = 1 ] && echo yes || echo no)" "exactly one request (got $(nreq))"
check "$([ "$(req 'r["path"]')" = "/hook?x=1" ] && echo yes || echo no)" "posted to the .env URL"
check "$([ "$(req 'r["body"]')" = '{"run_date":"2026-10-18","slot":"22:00","run_ts":"2026-10-18T16:44:03Z","overall":"OK","log":"'"$LOG"'"}' ] && echo yes || echo no)" "body is the agreed JSON object"
check "$([ "$(req 'r["headers"].get("Content-Type")')" = application/json ] && echo yes || echo no)" "Content-Type: application/json"
ts=$(req 'r["headers"].get("X-Paperclip-Timestamp")')
check "$([ "$ts" -ge "$before" ] && [ "$ts" -le $(( $(date +%s) + 1 )) ] && echo yes || echo no)" "X-Paperclip-Timestamp is now in unix seconds ($ts)"
expected=$(SECRET="$SECRET" "$PY" -c '
import hashlib, hmac, json, os, sys
r = [json.loads(l) for l in open(sys.argv[1])][-1]
ts = r["headers"]["X-Paperclip-Timestamp"]
msg = ts.encode() + b"." + r["body"].encode("latin-1")
print("sha256=" + hmac.new(os.environ["SECRET"].encode(), msg, hashlib.sha256).hexdigest())
' "$TMP/requests")
check "$([ "$(req 'r["headers"].get("X-Paperclip-Signature")')" = "$expected" ] && echo yes || echo no)" "signature = HMAC-SHA256(secret, ts.body) over the bytes received"
check "$(last_log | grep -q 'review webhook: sent (overall=OK, HTTP 202)' && echo yes || echo no)" "one 'sent' log line"
check "$([ "$(wc -l < "$LOG" | tr -d ' ')" = 1 ] && echo yes || echo no)" "exactly one log line"
check "$(grep -qF "s3cr3t" "$LOG" || grep -qF "127.0.0.1" "$LOG"; [ $? -ne 0 ] && echo yes || echo no)" "neither the secret nor the URL is logged"
check "$([ ! -e "$TMP/pwned" ] && echo yes || echo no)" ".env is not sourced (no command ran)"

echo
echo "2. Other overall values pass through"
: > "$TMP/requests"
notify_review "STOPPED_BUDGET"
check "$([ "$(req 'json.loads(r["body"])["overall"]')" = STOPPED_BUDGET ] && echo yes || echo no)" "overall STOPPED_BUDGET"

echo
echo "3. Server error: retried twice, logged, still returns 0"
echo 503 > "$TMP/status"
: > "$TMP/requests"; : > "$LOG"
notify_review "FAILED"; rc=$?
check "$([ "$rc" -eq 0 ] && echo yes || echo no)" "returns 0"
check "$([ "$(nreq)" = 3 ] && echo yes || echo no)" "1 try + 2 retries (got $(nreq))"
check "$(last_log | grep -q 'review webhook: FAILED (curl exit 22, HTTP 503) - verdict unchanged' && echo yes || echo no)" "one FAILED log line"
echo 202 > "$TMP/status"

echo
echo "4. Unreachable URL: logged, returns 0"
write_env "http://127.0.0.1:1/hook"
: > "$LOG"
notify_review "OK"; rc=$?
check "$([ "$rc" -eq 0 ] && echo yes || echo no)" "returns 0"
check "$(last_log | grep -q 'review webhook: FAILED (curl exit 7' && echo yes || echo no)" "one FAILED log line (connection refused)"

echo
echo "5. Missing key or missing .env: skip silently with one log line"
printf 'JOBO_REVIEW_WEBHOOK_URL=http://127.0.0.1:%s/hook\n' "$PORT" > "$REPO/.env"
: > "$TMP/requests"; : > "$LOG"
notify_review "OK"; rc=$?
check "$([ "$rc" -eq 0 ] && [ "$(nreq)" = 0 ] && echo yes || echo no)" "no secret -> no request, returns 0"
check "$(last_log | grep -q 'review webhook: skipped' && [ "$(wc -l < "$LOG" | tr -d ' ')" = 1 ] && echo yes || echo no)" "one 'skipped' log line"
rm -f "$REPO/.env"
: > "$LOG"
notify_review "OK"
check "$([ "$(nreq)" = 0 ] && last_log | grep -q 'review webhook: skipped' && echo yes || echo no)" "no .env -> skipped"

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "$fail CHECK(S) FAILED"
exit 1
