#!/usr/bin/env bash
# Back up jobo's job history (data/jobs.db) to a PRIVATE GitHub repo.
#
# The code repo never tracks data/ (it holds every job you looked at and every
# application jobo sent). Instead this script COPIES data/jobs.db into a
# separate private repo and pushes it. Run it after a pipeline run, or let the
# pipeline call it. The live data/jobs.db is only ever read, never moved.
#
#   JOBO_DATA_REPO=you/jobo-data   (in .env or the environment; required)
#   JOBO_DATA_CLONE=<dir>          (optional; default data/.backup-repo)
#
# One-time setup (./setup.sh offers to do this):
#   gh repo create you/jobo-data --private
#   echo 'JOBO_DATA_REPO=you/jobo-data' >> .env
#
# Usage: scripts/backup_data.sh [-m "commit message"] [--dry-run]
#
# Safety: refuses to push unless `gh repo view` reports the repo as PRIVATE.
# Commits use the clone's own git identity (set it once in that clone:
# git -C data/.backup-repo config user.name/user.email).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MSG=""
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -m) MSG="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "usage: $0 [-m message] [--dry-run]" >&2; exit 64 ;;
  esac
done

if [ -z "${JOBO_DATA_REPO:-}" ] && [ -f "$ROOT/.env" ]; then
  JOBO_DATA_REPO="$(sed -n 's/^JOBO_DATA_REPO=//p' "$ROOT/.env" | tail -1 | tr -d '"'"'"' ')"
fi
if [ -z "${JOBO_DATA_REPO:-}" ]; then
  echo "backup_data: JOBO_DATA_REPO is not set (e.g. JOBO_DATA_REPO=you/jobo-data in .env)" >&2
  exit 2
fi
DB="$ROOT/data/jobs.db"
if [ ! -f "$DB" ]; then
  echo "backup_data: $DB not found" >&2
  exit 2
fi
CLONE="${JOBO_DATA_CLONE:-$ROOT/data/.backup-repo}"

vis="$(gh repo view "$JOBO_DATA_REPO" --json visibility -q .visibility 2>/dev/null || true)"
if [ "$vis" != "PRIVATE" ]; then
  echo "backup_data: $JOBO_DATA_REPO is not a private repo you can reach (visibility: ${vis:-unknown}); refusing to push" >&2
  exit 3
fi

if [ ! -d "$CLONE/.git" ]; then
  gh repo clone "$JOBO_DATA_REPO" "$CLONE" -- --quiet
fi
git -C "$CLONE" pull --quiet --ff-only 2>/dev/null || true

# A consistent snapshot even if a run is writing: sqlite's online backup when
# the sqlite3 module is available, else a plain copy.
python3 - "$DB" "$CLONE/jobs.db" <<'PY' || cp "$DB" "$CLONE/jobs.db"
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY

git -C "$CLONE" add jobs.db
if git -C "$CLONE" diff --cached --quiet; then
  echo "backup_data: jobs.db unchanged; nothing to push"
  exit 0
fi
[ -n "$MSG" ] || MSG="jobs.db backup $(date '+%Y-%m-%d %H:%M %Z')"
if [ "$DRY" -eq 1 ]; then
  echo "backup_data: dry run; would commit and push: $MSG"
  git -C "$CLONE" reset --quiet
  exit 0
fi
git -C "$CLONE" commit --quiet -m "$MSG"
git -C "$CLONE" push --quiet
echo "backup_data: pushed $(git -C "$CLONE" rev-parse --short HEAD) to $JOBO_DATA_REPO"
