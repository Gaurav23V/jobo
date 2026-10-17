#!/usr/bin/env bash
# Merge gate for auto-merged fixes (GHO-45). Run from a feature worktree:
#   bash scripts/premerge_check.sh [--base origin/main] [--no-new-test-ok]
# Checks BASE..HEAD and exits 0 only if every check passes (1 = a check
# failed, 64 = bad usage). One PASS/FAIL line per check. Never prints a matched
# secret or personal term: counts, rule ids, file names and commit ids only.
#
#   --no-new-test-ok  pure tooling/docs change: waive the new-test check
#                     (the waiver is printed).
#   --skip-tests      ONLY for scripts/test_premerge_check.sh; a run that used
#                     it never authorizes a merge.
#
# BASE is used as it is: `git fetch origin` first. The personal-terms list is
# <live checkout>/.privacy/terms.txt (gitignored, never printed or copied),
# where the live checkout is the parent of this repo's common git dir, so it is
# found from any worktree. A missing or empty list is a FAIL, never a skip.
set -u

BASE=origin/main
SKIP_TESTS=0
NO_NEW_TEST_OK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) [ $# -ge 2 ] || { echo "usage: $0 [--base REF] [--no-new-test-ok]" >&2; exit 64; }
            BASE="$2"; shift 2 ;;
    --base=*) BASE="${1#--base=}"; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --no-new-test-ok) NO_NEW_TEST_OK=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "premerge_check: unknown argument: $1" >&2
       echo "usage: $0 [--base REF] [--no-new-test-ok]" >&2; exit 64 ;;
  esac
done

IDENTITY_NAME='Gaurav23V'
IDENTITY_EMAIL='137498654+Gaurav23V@users.noreply.github.com'
ATTRIBUTION_RE='co-authored-by|generated with|claude|paperclip|noreply@anthropic\.com'
# Paths a PR may never add or modify (deleting them is fine).
FORBIDDEN_RE='(^|/)(personal|data|credentials|\.privacy)/|(^|/)\.env|\.(db|sqlite3?|pdf|tex)$'
FORBIDDEN_OK_RE='^\.env\.example$'

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "premerge_check: not inside a git repo" >&2; exit 64; }
cd "$ROOT"
BASE_SHA=$(git rev-parse --verify -q "$BASE^{commit}") || { echo "premerge_check: unknown base: $BASE" >&2; exit 64; }
HEAD_SHA=$(git rev-parse HEAD)
COMMON=$(git rev-parse --path-format=absolute --git-common-dir)
LIVE=$(dirname "$COMMON")
TERMS="$LIVE/.privacy/terms.txt"
GITLEAKS="${GITLEAKS:-$(command -v gitleaks || echo "$HOME/.local/bin/gitleaks")}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
fail=0
pass() { printf 'PASS  %-10s %s\n' "$1" "$2"; }
flunk() { printf 'FAIL  %-10s %s\n' "$1" "$2"; fail=1; }
# Path names can hold a personal term too: withhold any name that matches the
# terms list before it is printed (stdin: one name per line, stdout: one line).
TERMS_OK=0
[ -f "$TERMS" ] && grep -q '[^[:space:]]' "$TERMS" && TERMS_OK=1
names() {
  local p out=()
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ "$TERMS_OK" -eq 1 ] && printf '%s\n' "$p" | rg -q -i -P -f "$TERMS" 2>/dev/null; then
      out+=("<name withheld: personal term>")
    else out+=("$p"); fi
  done
  echo "${out[*]}"
}

N=$(git rev-list --count "$BASE_SHA..$HEAD_SHA")
echo "premerge_check: $BASE (${BASE_SHA:0:9})..HEAD (${HEAD_SHA:0:9}), $N commit(s), in $ROOT"
if [ "$N" -eq 0 ]; then
  flunk range "no commits in $BASE..HEAD: nothing to merge"
  echo "PREMERGE: FAIL"
  exit 1
fi

# The tests below run on the working tree, so it must be what is committed.
DIRTY=$(git status --porcelain --untracked-files=no | wc -l)
if [ "$DIRTY" -eq 0 ]; then pass clean "no uncommitted changes to tracked files"
else flunk clean "$DIRTY tracked file(s) with uncommitted changes: commit or stash first"; fi

# 1. Identity and attribution.
bad_id=$(git log --format='%h%x09%an <%ae>%x09%cn <%ce>' "$BASE_SHA..$HEAD_SHA" |
  awk -F'\t' -v want="$IDENTITY_NAME <$IDENTITY_EMAIL>" '$2 != want || $3 != want {print $1}')
bad_msg=""
for c in $(git rev-list "$BASE_SHA..$HEAD_SHA"); do
  if git log -1 --format=%B "$c" | grep -qiE "$ATTRIBUTION_RE"; then bad_msg+="${c:0:7} "; fi
done
if [ -z "$bad_id" ] && [ -z "$bad_msg" ]; then
  pass identity "$N commit(s) authored and committed as $IDENTITY_NAME, no attribution"
else
  [ -n "$bad_id" ] && flunk identity "wrong author/committer on: $(echo $bad_id)"
  [ -n "$bad_msg" ] && flunk identity "attribution text (co-author/generated/AI/Paperclip) in message of: $bad_msg"
fi

# 2. Secrets (gitleaks over the commits in the range; report kept in TMP).
if [ ! -x "$GITLEAKS" ]; then
  flunk secrets "gitleaks not found (looked for $GITLEAKS)"
else
  "$GITLEAKS" git --log-opts="$BASE_SHA..$HEAD_SHA" --no-banner --redact \
    --report-format json --report-path "$TMP/leaks.json" . >/dev/null 2>&1
  rc=$?
  leaks=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])) or []))' "$TMP/leaks.json" 2>/dev/null || echo "?")
  if [ "$rc" -eq 0 ] && [ "$leaks" = 0 ]; then
    pass secrets "gitleaks: no findings in $N commit(s)"
  elif [ "$leaks" = "?" ] || [ "$leaks" = 0 ]; then
    flunk secrets "gitleaks did not complete (exit $rc)"
  else
    where=$(python3 -c 'import json,sys
print("\n".join(sorted({f["File"] for f in json.load(open(sys.argv[1]))})))' "$TMP/leaks.json" | names)
    rules=$(python3 -c 'import json,sys
print(" ".join(sorted({f["RuleID"] for f in json.load(open(sys.argv[1]))})))' "$TMP/leaks.json")
    flunk secrets "gitleaks: $leaks finding(s), rules: $rules; files: $where"
  fi
fi

# 3. Personal terms in what the PR adds: added diff lines, new path names and
#    commit messages. rg stderr is dropped because a pattern error echoes the
#    pattern; a nonzero count or an rg error is a FAIL.
if [ ! -f "$TERMS" ]; then
  flunk terms "terms list missing at $TERMS"
elif [ "$TERMS_OK" -ne 1 ]; then
  flunk terms "terms list at $TERMS is empty"
else
  hits=""; rg_err=0
  scan() {  # scan <label> <file>
    local n
    n=$(rg -c -i -P -f "$TERMS" -- "$2" 2>/dev/null); local rc=$?
    if [ "$rc" -eq 0 ]; then hits+="$1: $n; "; elif [ "$rc" -ne 1 ]; then rg_err=1; fi
  }
  git -c core.quotePath=false diff --name-only --no-renames --diff-filter=ACMRT "$BASE_SHA...$HEAD_SHA" > "$TMP/paths"
  scan "(file names)" "$TMP/paths"
  git log --format=%B "$BASE_SHA..$HEAD_SHA" > "$TMP/messages"
  scan "(commit messages)" "$TMP/messages"
  while IFS= read -r f; do
    git diff -U0 --text --no-color --no-ext-diff "$BASE_SHA...$HEAD_SHA" -- "$f" |
      awk '/^diff --git /{h=0; next} /^@@ /{h=1; next} h && /^\+/ {print substr($0, 2)}' > "$TMP/added"
    scan "$(printf '%s\n' "$f" | names)" "$TMP/added"
  done < "$TMP/paths"
  if [ "$rg_err" -ne 0 ]; then flunk terms "rg could not run the terms list (bad pattern?)"
  elif [ -n "$hits" ]; then flunk terms "personal-term hits in added content: ${hits%; }"
  else pass terms "no personal-term hits in added lines of $(wc -l < "$TMP/paths") file(s), file names or messages"; fi
fi

# 4. Forbidden paths.
forbidden=$(git -c core.quotePath=false diff --name-only --no-renames --diff-filter=ACMRT "$BASE_SHA...$HEAD_SHA" |
  grep -E "$FORBIDDEN_RE" | grep -vE "$FORBIDDEN_OK_RE")
if [ -z "$forbidden" ]; then pass paths "nothing under personal/ data/ credentials/ .privacy/, no .env*/db/sqlite/pdf/tex"
else flunk paths "forbidden path(s) added or modified: $(names <<<"$forbidden")"; fi

# 5. Regression suite.
if [ "$SKIP_TESTS" -eq 1 ]; then
  echo "SKIP  tests      --skip-tests used: this run does NOT authorize a merge"
else
  echo "....  tests      running bash scripts/run_regression_tests.sh (about 10 min)"
  LOG=$(mktemp /tmp/jobo-premerge-tests.XXXXXX.log)
  if bash scripts/run_regression_tests.sh > "$LOG" 2>&1; then
    pass tests "regression suite green (log: $LOG)"
  else
    flunk tests "regression suite failed (log: $LOG)"
    tail -n 15 "$LOG" > "$TMP/tail"
    if [ "$TERMS_OK" -eq 1 ]; then
      rg -v -i -P -f "$TERMS" "$TMP/tail" 2>/dev/null | sed 's/^/      | /'
      echo "      (last 15 log lines; any line with a personal term is left out)"
    fi
  fi
fi

# 6. A regression test is added or changed.
new_tests=$(git -c core.quotePath=false diff --name-only --diff-filter=AM "$BASE_SHA...$HEAD_SHA" | grep -E '^scripts/test_' || true)
if [ -n "$new_tests" ]; then pass new-test "$(names <<<"$new_tests")"
elif [ "$NO_NEW_TEST_OK" -eq 1 ]; then pass new-test "waived: --no-new-test-ok used (tooling/docs change, no scripts/test_* file)"
else flunk new-test "no scripts/test_* file added or modified (use --no-new-test-ok only for tooling/docs)"; fi

if [ "$fail" -ne 0 ]; then echo "PREMERGE: FAIL"; exit 1; fi
if [ "$SKIP_TESTS" -eq 1 ]; then echo "PREMERGE: PASS (tests skipped: not valid for a merge)"
else echo "PREMERGE: PASS"; fi
exit 0
