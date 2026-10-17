#!/usr/bin/env bash
# Drives the REAL scripts/premerge_check.sh (GHO-45) against throwaway git
# repos in a temp dir, each with a fake .privacy/terms.txt, and checks that
# every gate fails on a planted violation and passes on a clean commit. Never
# touches the real repo, the real terms list or the network:
#   - each case is a fresh repo: "main" is the base, "feat" the PR branch;
#   - the repo is its own live checkout, so the fake terms list is the one
#     premerge_check finds (case 15 runs from a linked worktree to prove the
#     lookup goes through the common git dir);
#   - scripts/run_regression_tests.sh is a stub whose exit code is scripted;
#   - planted secrets are generated at run time, so this file holds none.
set -u
REAL_REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$REAL_REPO/scripts/premerge_check.sh"
export GITLEAKS="${GITLEAKS:-$(command -v gitleaks || echo "$HOME/.local/bin/gitleaks")}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home" "$TMP/bin"
# Plain git with no inherited identity or config: a git wrapper on PATH (or
# GIT_AUTHOR_*/GIT_COMMITTER_*/GIT_CONFIG_* in the env) could force one
# identity onto every commit and hide the bad-author/committer cases. Identity
# comes from each throwaway repo's own config.
ln -s "$(command -pv git)" "$TMP/bin/git"
export PATH="$TMP/bin:$PATH" HOME="$TMP/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
for v in $(compgen -e | grep -E '^GIT_(AUTHOR_|COMMITTER_|CONFIG_(COUNT|KEY_|VALUE_|PARAMETERS))'); do unset "$v"; done
ME_NAME=Gaurav23V ME_EMAIL=137498654+Gaurav23V@users.noreply.github.com

TERM_WORD=zebraquillfake
FAKE_KEY="AKIA$(LC_ALL=C tr -dc 'A-Z2-7' < /dev/urandom | head -c 16)"
fail=0
check() { if [ "$1" = yes ]; then echo "  ok   $2"; else echo "  FAIL $2"; fail=1; fi; }

# new_repo <name>: base commit on main with a planted term in an existing line
# (so removing it is not an added-line hit), then switch to branch feat.
new_repo() {
  R="$TMP/$1"; mkdir -p "$R/scripts" "$R/.privacy"
  git -C "$R" init -q -b main
  git -C "$R" config user.name "$ME_NAME"; git -C "$R" config user.email "$ME_EMAIL"
  printf '.privacy/\n' > "$R/.gitignore"
  printf 'readme\nold line mentions %s\n' "$TERM_WORD" > "$R/README.md"
  printf '#!/bin/bash\nexit "$(cat "$(dirname "$0")/../.stub_rc" 2>/dev/null || echo 0)"\n' \
    > "$R/scripts/run_regression_tests.sh"
  printf '%s\nfake\\s?employer\\d+\n' "$TERM_WORD" > "$R/.privacy/terms.txt"
  git -C "$R" add -A && git -C "$R" commit -q -m "base"
  git -C "$R" checkout -q -b feat
}
# commit_file <path> <content> [message] [extra git-commit args...]
commit_file() {
  mkdir -p "$(dirname "$R/$1")"; printf '%s\n' "$2" > "$R/$1"
  git -C "$R" add -f -- "$1"
  git -C "$R" commit -q -m "${3:-add $1}" "${@:4}"
}
clean_test() { commit_file scripts/test_widget.py "print('widget ok')" "Add widget regression test"; }
# run_check [args...]: runs from $R (or $RUN_DIR), output in $OUT, exit in $RC
run_check() {
  (cd "${RUN_DIR:-$R}" && bash "$CHECK" --base main "$@") > "$TMP/out" 2>&1
  RC=$?; OUT=$(cat "$TMP/out")
}
line() { grep -E "^$1 +$2 " <<<"$OUT" | head -1; }
has() { [ -n "$(line "$1" "$2")" ] && echo yes || echo no; }
leaked() { grep -qiF -e "$TERM_WORD" -e "$FAKE_KEY" <<<"$OUT" && echo yes || echo no; }
show_on_fail() { [ "$fail" -eq 0 ] || sed 's/^/      | /' "$TMP/out"; }

[ -x "$GITLEAKS" ] || { echo "gitleaks not found at $GITLEAKS"; exit 1; }

echo "1. Clean commit passes every check"
new_repo clean; clean_test
run_check --skip-tests
check "$([ "$RC" -eq 0 ] && echo yes || echo no)" "exit 0 (rc=$RC)"
for c in clean identity secrets terms paths new-test; do check "$(has PASS "$c")" "PASS $c"; done
check "$(grep -q '^SKIP  tests .*does NOT authorize a merge' <<<"$OUT" && echo yes || echo no)" "skip-tests is announced as not valid for a merge"
check "$(grep -q '^PREMERGE: PASS (tests skipped' <<<"$OUT" && echo yes || echo no)" "summary flags the skipped tests"
show_on_fail

echo "2. Wrong author fails identity"
new_repo author; clean_test; commit_file scripts/a.py "x = 1" "tweak" --author "Someone Else <someone@example.com>"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL identity)" = yes ] && echo yes || echo no)" "FAIL identity, exit 1 (rc=$RC)"

echo "3. Wrong committer fails identity"
new_repo committer; clean_test
printf 'x = 1\n' > "$R/scripts/a.py"; git -C "$R" add scripts/a.py
git -C "$R" -c user.email=bot@example.com commit -q -m "tweak" --author "$ME_NAME <$ME_EMAIL>"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL identity)" = yes ] && echo yes || echo no)" "FAIL identity, exit 1 (rc=$RC)"

echo "4. Attribution trailers and AI/Paperclip mentions fail identity"
for msg in $'fix\n\nCo-Authored-By: Someone <x@example.com>' "fix (generated with a tool)" \
           "fix flagged by CLAUDE" "fix from paperclip run" $'fix\n\ncc noreply@anthropic.com'; do
  new_repo attrib; clean_test; commit_file scripts/a.py "x = 1" "$msg"
  run_check --skip-tests
  check "$([ "$RC" -eq 1 ] && [ "$(has FAIL identity)" = yes ] && echo yes || echo no)" "FAIL identity for message: ${msg%%$'\n'*}..."
  rm -rf "$TMP/attrib"
done
new_repo trailer; clean_test
commit_file scripts/a.py "x = 1" "tweak" --trailer "Co-authored-by: Bot <bot@example.com>"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL identity)" = yes ] && echo yes || echo no)" "FAIL identity for a git --trailer co-author"

echo "5. A fake AWS key fails secrets, and is never printed"
new_repo secret; clean_test; commit_file module/settings.py "aws_access_key_id = \"$FAKE_KEY\""
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL secrets)" = yes ] && echo yes || echo no)" "FAIL secrets, exit 1 (rc=$RC)"
check "$(line FAIL secrets | grep -q 'module/settings.py' && echo yes || echo no)" "names the file"
check "$([ "$(leaked)" = no ] && echo yes || echo no)" "key not in output"

echo "6. A planted term in an added line fails terms, with a count and no value"
new_repo term; clean_test; commit_file scripts/notes.py "# applied at Fake Employer42 today"$'\n'"ok = True"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL terms)" = yes ] && echo yes || echo no)" "FAIL terms, exit 1 (rc=$RC)"
check "$(line FAIL terms | grep -q 'scripts/notes.py: 1' && echo yes || echo no)" "per-file count scripts/notes.py: 1"
check "$(grep -qi 'employer42' <<<"$OUT" && echo no || echo yes)" "term not in output"

echo "7. Only added lines count: removing a term passes, keeping it unchanged passes"
new_repo removed; clean_test
printf 'readme\n' > "$R/README.md"; git -C "$R" commit -q -am "drop old line"
commit_file other.txt "unrelated"
run_check --skip-tests
check "$([ "$RC" -eq 0 ] && [ "$(has PASS terms)" = yes ] && echo yes || echo no)" "PASS terms (rc=$RC)"

echo "8. Terms in a new file name or a commit message fail terms"
new_repo name; clean_test; commit_file "docs/${TERM_WORD}_notes.md" "met Fake Employer9"
run_check --skip-tests
check "$(line FAIL terms | grep -q '(file names): 1' && echo yes || echo no)" "file name hit counted"
check "$(line FAIL terms | grep -qF '<name withheld: personal term>: 1' && echo yes || echo no)" "content hit reported under a withheld name"
check "$([ "$(leaked)" = no ] && echo yes || echo no)" "term not in output (file name withheld)"
new_repo msg; clean_test; commit_file scripts/a.py "x = 1" "follow up for $TERM_WORD"
run_check --skip-tests
check "$(line FAIL terms | grep -q '(commit messages): 1' && echo yes || echo no)" "commit message hit counted"

echo "9. Missing or empty terms list fails, never skips"
new_repo noterms; clean_test; rm "$R/.privacy/terms.txt"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && line FAIL terms | grep -q 'missing' && echo yes || echo no)" "missing -> FAIL terms (rc=$RC)"
: > "$R/.privacy/terms.txt"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && line FAIL terms | grep -q 'empty' && echo yes || echo no)" "empty -> FAIL terms (rc=$RC)"
printf 'bad(pattern\n' > "$R/.privacy/terms.txt"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && line FAIL terms | grep -q 'could not run' && echo yes || echo no)" "bad pattern -> FAIL terms, pattern not echoed"
check "$(grep -q 'bad(pattern' <<<"$OUT" && echo no || echo yes)" "pattern text not in output"

echo "10. Forbidden paths fail"
for p in personal/profile.yaml data/jobs.db credentials/token.json .privacy/extra.txt .env.local \
         nested/.env cache.sqlite resume.pdf docs/cv.tex; do
  new_repo forbid; clean_test; commit_file "$p" "x"
  run_check --skip-tests
  check "$([ "$RC" -eq 1 ] && line FAIL paths | grep -qF "$p" && echo yes || echo no)" "FAIL paths for $p"
  rm -rf "$TMP/forbid"
done
new_repo envexample; clean_test; commit_file .env.example "NEW_SETTING="
run_check --skip-tests
check "$([ "$(has PASS paths)" = yes ] && echo yes || echo no)" ".env.example template is allowed"
new_repo deleted; git -C "$R" checkout -q main; commit_file data/old.txt "x"; git -C "$R" checkout -q -B feat
git -C "$R" rm -q data/old.txt && git -C "$R" commit -q -m "remove stray data file"; clean_test
run_check --skip-tests
check "$([ "$(has PASS paths)" = yes ] && echo yes || echo no)" "deleting a file under data/ is allowed"

echo "11. No new scripts/test_* file fails, unless waived (and the waiver is printed)"
new_repo notest; commit_file docs/howto.md "steps"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL new-test)" = yes ] && echo yes || echo no)" "FAIL new-test (rc=$RC)"
run_check --skip-tests --no-new-test-ok
check "$([ "$RC" -eq 0 ] && line PASS new-test | grep -q 'waived: --no-new-test-ok used' && echo yes || echo no)" "waiver passes and is printed (rc=$RC)"

echo "12. Tests run unless skipped: a red suite fails, a green one passes"
new_repo suite; clean_test
echo 1 > "$R/.stub_rc"; git -C "$R" add -f .stub_rc; git -C "$R" commit -q -m "red suite"
run_check
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL tests)" = yes ] && echo yes || echo no)" "FAIL tests (rc=$RC)"
echo 0 > "$R/.stub_rc"; git -C "$R" commit -q -am "green suite"
run_check
check "$([ "$RC" -eq 0 ] && [ "$(has PASS tests)" = yes ] && grep -q '^PREMERGE: PASS$' <<<"$OUT" && echo yes || echo no)" "PASS tests, PREMERGE: PASS (rc=$RC)"

echo "13. Uncommitted tracked changes fail (tests would not test HEAD)"
new_repo dirty; clean_test; echo "edit" >> "$R/README.md"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL clean)" = yes ] && echo yes || echo no)" "FAIL clean (rc=$RC)"

echo "14. Empty range and bad usage"
new_repo empty
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && [ "$(has FAIL range)" = yes ] && echo yes || echo no)" "no commits -> FAIL range (rc=$RC)"
run_check --bogus
check "$([ "$RC" -eq 64 ] && echo yes || echo no)" "unknown argument -> exit 64 (rc=$RC)"
(cd "$R" && bash "$CHECK" --base no-such-ref) > /dev/null 2>&1; RC=$?
check "$([ "$RC" -eq 64 ] && echo yes || echo no)" "unknown base -> exit 64 (rc=$RC)"

echo "15. From a linked worktree the terms list comes from the live checkout"
new_repo live; git -C "$R" checkout -q main
git -C "$R" worktree add -q -b feat2 "$TMP/live-wt" main
RUN_DIR="$TMP/live-wt"
printf '# hello Fake Employer7\n' > "$RUN_DIR/scripts/test_x.py"
git -C "$RUN_DIR" add scripts/test_x.py && git -C "$RUN_DIR" commit -q -m "add test"
check "$([ -e "$RUN_DIR/.privacy/terms.txt" ] && echo no || echo yes)" "worktree has no terms list of its own"
run_check --skip-tests
check "$([ "$RC" -eq 1 ] && line FAIL terms | grep -q 'scripts/test_x.py: 1' && echo yes || echo no)" "hit found via the live checkout's list (rc=$RC)"
unset RUN_DIR

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "CHECK(S) FAILED; last output:"
sed -n '1,40p' "$TMP/out"
exit 1
