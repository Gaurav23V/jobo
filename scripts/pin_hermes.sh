#!/usr/bin/env bash
# Pin the Hermes the apply agent runs (GHO-13). Usage: scripts/pin_hermes.sh [COMMIT]
#
# Exports COMMIT (default 71d34bad10, the last Hermes before the unified
# gateway runtime; it ran every apply from 13 to 15 Oct 2026) from the
# auto-updated ~/.hermes/hermes-agent checkout into
# ~/.hermes/jobo-hermes-pin/<commit>, copies Hermes' current dependency venv
# next to it, checks the tree still runs turns in-process, and points
# ~/.hermes/jobo-hermes-pin/current at it. scripts/hermes_pinned.sh runs it;
# module4/agent_driver.py uses it whenever it exists.
#
# The pin never changes on its own; Hermes' daily auto-update only moves
# ~/.hermes/hermes-agent. Only the apply agent uses the pin: triage, the user's own
# Hermes and its gateway are untouched.
set -euo pipefail
SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
COMMIT="${1:-71d34bad1045610252eda02ee79549436896e784}"
BASE="$HOME/.hermes/jobo-hermes-pin"
FULL="$(git -C "$SRC" rev-parse --verify "$COMMIT^{commit}")"
DEST="$BASE/${FULL:0:10}"

if grep -q "GatewayAgent as AIAgent" <(git -C "$SRC" show "$FULL:hermes_cli/cli_agent_setup_mixin.py" 2>/dev/null); then
  echo "pin_hermes: $FULL runs turns in the shared gateway; the apply agent cannot use it" >&2
  exit 1
fi

# The dependency venv Hermes selected for its install (pm/environments.py).
VENV="$(cd "$SRC" && "$(sed -n 's#^exec \(/[^ ]*python3\) .*#\1#p' "$SRC/.hermes/bin/hermes" | head -1)" -I -c '
import sys; sys.path.insert(0, ".")
from pathlib import Path
from pm import environments as e
print(e.selected_venv(Path(".").resolve()))')"
[ -x "$VENV/bin/python" ] || { echo "pin_hermes: no dependency venv found ($VENV)" >&2; exit 1; }

mkdir -p "$BASE"
TMP="$(mktemp -d "$BASE/.pin.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
git -C "$SRC" archive "$FULL" | tar -x -C "$TMP"
cp -a "$VENV" "$TMP/.venv"
rm -rf "$DEST"
mv "$TMP" "$DEST"
trap - EXIT
ln -sfn "$(basename "$DEST")" "$BASE/current"

JOBO_HERMES_PIN_DIR="$DEST" "$(dirname "$0")/hermes_pinned.sh" --version | head -2
echo "pinned: $BASE/current -> $(basename "$DEST")"
