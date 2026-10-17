#!/usr/bin/env bash
# One-command setup for jobo: tools, your details, resume, logins, schedule.
# Safe to re-run; it shows current values as defaults and asks before
# replacing any file. Ends with `jobo doctor`. Details: jobo_setup/wizard.py.
#
#   ./setup.sh                                      interactive
#   ./setup.sh --non-interactive --answers FILE     no prompts (JSON; see
#                                                   setup.answers.example.json)
#   ./setup.sh --skip-installs                      only check system software
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

skip=0 interactive=1
for arg in "$@"; do
  case "$arg" in
    --skip-installs) skip=1 ;;
    --non-interactive) interactive=0 ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    PATH="$HOME/.local/bin:$PATH"
  elif [[ $skip == 0 && $interactive == 1 ]] \
      && read -rp "uv is not installed. Install it now (curl -LsSf https://astral.sh/uv/install.sh | sh)? [Y/n] " reply \
      && [[ ! "$reply" =~ ^[nN] ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
  else
    echo "setup.sh: uv is required. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
fi

echo "== Python dependencies (uv sync) =="
uv sync
# A PYTHONPATH from another tool (e.g. a Hermes terminal) would shadow .venv.
exec env -u PYTHONPATH uv run --no-sync python -m jobo_setup.wizard "$@"
