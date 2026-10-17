#!/usr/bin/env bash
# Move an existing (pre-public) jobo install onto the clean public history,
# keeping its data, logins and local branches. --undo puts it back.
#
# Before jobo went public its history held personal data, so the public repo
# was restarted from one clean commit. An install made before that has:
#   * a local `main` that no longer matches origin/main,
#   * tracked personal files (data/jobs.db, module3/templates/resume.*,
#     module3/context/profile.md) that the new history does not have,
#   * no personal/ config (the new code reads the candidate from there).
#
# `git checkout`/`git reset --hard` onto the new history would DELETE those
# untracked-in-new files from disk. This script never does that:
#   1. refuses while a pipeline run is active or /tmp/jobo_pipeline.lock is held;
#   2. writes a manifest + backups under data/switch_over/<stamp>/ (the old
#      HEAD, the old branch, sha256 of every file it might touch);
#   3. keeps the old main reachable as branch `pre-public-main`;
#   4. creates personal/ from the old revision's hard-coded values
#      (scripts/migrate_local_config.py; nothing is moved);
#   5. fetches origin and points the current branch at origin/main with
#      `git reset --mixed` (moves the branch only; no file is touched);
#   6. writes the new tree's tracked files (`git checkout -- .` for paths in
#      the NEW tree only) and removes the few CODE files the old tree had and
#      the new one doesn't. data/, personal/, .env, credentials/ and anything
#      gitignored are never removed. Old personal files that the new code no
#      longer reads (module3/templates/, module3/context/profile.md) are left
#      on disk untouched.
#
# Usage:
#   scripts/switch_over.sh [--repo DIR] [--remote origin] [--branch main] [--dry-run]
#   scripts/switch_over.sh --undo [--repo DIR]   # restores the newest manifest
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO=""
REMOTE="origin"
BRANCH="main"
DRY=0
UNDO=0
LOCK="${JOBO_PIPELINE_LOCK:-/tmp/jobo_pipeline.lock}"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --undo) UNDO=1; shift ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
[ -n "$REPO" ] || REPO="$(cd "$SELF_DIR/.." && pwd)"
REPO="$(cd "$REPO" && pwd)"
cd "$REPO"
git rev-parse --git-dir >/dev/null

say() { echo "switch_over: $*"; }
die() { echo "switch_over: ERROR: $*" >&2; exit 1; }
run() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

pipeline_busy() {
  # Never take the lock, not even briefly (a probe could make a cron run skip
  # its slot): look for a process that has it open instead (read-only /proc).
  if [ -e "$LOCK" ]; then
    local fd
    for fd in /proc/[0-9]*/fd/*; do
      [ "$(readlink "$fd" 2>/dev/null)" = "$LOCK" ] && return 0
    done
  fi
  [ -d "$LOCK.d" ] && return 0
  # A pipeline process: one argv entry is a pipeline script, or is `jobo` /
  # main.py followed by a pipeline subcommand. Matching whole argv entries
  # (not `pgrep -f` on the joined command line) avoids false hits on shells
  # or tools whose command text merely mentions these names.
  python3 -I - "$$" <<'PY'
import os, sys
me = int(sys.argv[1])
scripts = {"daily_pipeline.sh", "launch_pipeline.sh", "run_job_batch.sh"}
subs = {"apply", "triage", "enrich", "ingest", "run"}
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) in (me, os.getpid(), os.getppid()):
        continue
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    args = [a.decode("utf-8", "replace") for a in args if a]
    base = [os.path.basename(a) for a in args]
    if any(b in scripts for b in base):
        sys.exit(0)
    for i, b in enumerate(base[:-1]):
        if b in ("jobo", "main.py") and args[i + 1] in subs:
            sys.exit(0)
sys.exit(1)
PY
}

if pipeline_busy; then
  die "a jobo pipeline run is active (or holds $LOCK); try again after it finishes"
fi
if [ -n "$(git diff --cached --name-only)" ]; then
  die "the index has staged changes; commit or unstage them first"
fi

# Paths that are data or personal: never deleted, never overwritten.
KEEP_RE='^(data/|personal/|credentials/|\.env$|module3/templates/|module3/context/profile\.md$|docs/paused_run_)'

# ------------------------------------------------------------------ undo
if [ "$UNDO" -eq 1 ]; then
  M="$(ls -1d "$REPO"/data/switch_over/*/ 2>/dev/null | sort | tail -1)"
  [ -n "$M" ] && [ -f "$M/manifest.env" ] || die "no switch-over manifest under data/switch_over/"
  # shellcheck disable=SC1091
  . "$M/manifest.env"
  say "undo: restoring branch $OLD_BRANCH to $OLD_HEAD (manifest $M)"
  run git reset --quiet --mixed "$OLD_HEAD"
  # Restore the old tree's CODE files. Never data/ or the old personal files:
  # data/jobs.db was tracked in the old history, and a plain `checkout -- .`
  # would replace the live db (with runs since the switch) by the old copy.
  mapfile -t RESTORE < <(git ls-tree -r --name-only "$OLD_HEAD" | grep -Ev "$KEEP_RE")
  if [ "${#RESTORE[@]}" -gt 0 ]; then
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] git checkout -- <${#RESTORE[@]} old code files>"
    else printf '%s\0' "${RESTORE[@]}" | git checkout --pathspec-from-file=- --pathspec-file-nul -- ; fi
  fi
  # Remove files that only the new tree had (code the switch brought in).
  if [ -f "$M/added_files" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      [[ "$f" =~ $KEEP_RE ]] && continue
      git cat-file -e "$OLD_HEAD:$f" 2>/dev/null && continue
      [ -e "$f" ] && run rm -f -- "$f"
      # drop directories the switch created and that are now empty
      d="$(dirname -- "$f")"
      while [ "$d" != "." ] && [ "$DRY" -eq 0 ] && rmdir -- "$d" 2>/dev/null; do d="$(dirname -- "$d")"; done
    done < "$M/added_files"
  fi
  if [ -f "$M/created_personal" ] && [ -d "$REPO/personal" ]; then
    run mv "$REPO/personal" "$M/personal.undone"
    say "undo: moved the generated personal/ to $M/personal.undone"
  fi
  if [ -f "$M/remote_url" ]; then
    run git remote set-url "$REMOTE" "$(cat "$M/remote_url")"
  fi
  [ "$DRY" -eq 1 ] || mv "$M" "${M%/}.undone"
  say "undo: done. HEAD is $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
  exit 0
fi

# ------------------------------------------------------------------ forward
OLD_HEAD="$(git rev-parse HEAD)"
OLD_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$OLD_BRANCH" != "HEAD" ] || die "detached HEAD; check out your main branch first"
# (no `| grep -q`: with pipefail, grep exiting early kills git show with SIGPIPE)
OLD_PROMPTS="$(git show "$OLD_HEAD:module4/prompts.py" 2>/dev/null || true)"
grep -q '^CANDIDATE_FACTS = """' <<<"$OLD_PROMPTS" \
  || die "HEAD ($OLD_BRANCH) does not look like a pre-public jobo (already switched?)"

STAMP="$(date +%Y%m%d_%H%M%S)"
M="$REPO/data/switch_over/$STAMP"
say "backup manifest: $M"
if [ "$DRY" -eq 0 ]; then
  mkdir -p "$M"
  {
    echo "OLD_HEAD=$OLD_HEAD"
    echo "OLD_BRANCH=$OLD_BRANCH"
  } > "$M/manifest.env"
  git remote get-url "$REMOTE" > "$M/remote_url" 2>/dev/null || true
  # Fingerprints of the things that must survive (never their contents).
  (for p in data/jobs.db .env credentials/client_secret.json credentials/token.json; do
     [ -e "$p" ] && sha256sum "$p"; done) > "$M/protected.sha256" || true
fi

if git show-ref --verify --quiet refs/heads/pre-public-main; then
  say "branch pre-public-main already exists; leaving it"
else
  run git branch pre-public-main "$OLD_HEAD"
  say "old $OLD_BRANCH kept as branch pre-public-main ($OLD_HEAD)"
fi

if [ -d personal ]; then
  say "personal/ already exists; leaving it"
else
  run python3 "$SELF_DIR/migrate_local_config.py" --repo "$REPO" --ref "$OLD_HEAD" --out "$REPO/personal"
  [ "$DRY" -eq 1 ] || : > "$M/created_personal"
fi

run git fetch --quiet "$REMOTE"
NEW="$(git rev-parse "$REMOTE/$BRANCH")"
say "new history: $REMOTE/$BRANCH = $NEW"
git merge-base --is-ancestor "$NEW" "$OLD_HEAD" 2>/dev/null && die "$REMOTE/$BRANCH is already in $OLD_BRANCH's history; nothing to switch"

# Files the old tree tracked that the new tree doesn't. Keep anything that is
# data or personal; delete only code files the new tree has dropped.
mapfile -t GONE < <(comm -23 <(git ls-tree -r --name-only "$OLD_HEAD" | sort) \
                            <(git ls-tree -r --name-only "$NEW" | sort))
REMOVE=()
for f in "${GONE[@]}"; do
  [[ "$f" =~ $KEEP_RE ]] && continue
  REMOVE+=("$f")
done
if [ "$DRY" -eq 0 ]; then
  printf '%s\n' "${REMOVE[@]}" > "$M/removed_files"
  comm -13 <(git ls-tree -r --name-only "$OLD_HEAD" | sort) \
           <(git ls-tree -r --name-only "$NEW" | sort) > "$M/added_files"
fi

run git reset --quiet --mixed "$NEW"
# Write the new tree's files. Paths only the old tree had are untracked now
# and are not touched by this.
run git checkout -- .
for f in "${REMOVE[@]}"; do
  [ -e "$f" ] && run rm -f -- "$f"
done
say "removed ${#REMOVE[@]} code files the new tree no longer has (list: $M/removed_files)"

if [ "$DRY" -eq 0 ]; then
  ok=1
  while read -r sum path; do
    now="$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1)"
    [ "$now" = "$sum" ] || { echo "switch_over: CHANGED: $path" >&2; ok=0; }
  done < "$M/protected.sha256"
  [ "$ok" -eq 1 ] && say "data/jobs.db, .env and credentials/ unchanged"
fi
say "done. $OLD_BRANCH now follows $REMOTE/$BRANCH ($(git rev-parse --short HEAD)); undo with: $0 --undo"
