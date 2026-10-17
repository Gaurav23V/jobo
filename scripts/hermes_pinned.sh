#!/bin/sh
# The `hermes` the apply agent runs: a pinned Hermes tree, not the
# auto-updated ~/.hermes/hermes-agent (GHO-13).
#
# Hermes' unified gateway runtime (installed by the 15 Oct 2026 07:01 auto-
# update) runs every `hermes chat` turn inside one shared gateway process.
# The per-agent env jobo sets (BROWSER_CDP_URL, JOBO_SOURCE_URL, JOBO_UPLOAD_*)
# never reaches the tools there, so every apply agent drove a fresh local
# headless browser instead of its worker's CDP browser and nothing submitted.
# The pinned tree runs the turn in this process, as before.
#
# Create or move the pin with scripts/pin_hermes.sh. The tree holds a
# `.venv` copied from the Hermes dependency environment of that commit.
PIN="${JOBO_HERMES_PIN_DIR:-$HOME/.hermes/jobo-hermes-pin/current}"
if [ ! -x "$PIN/.venv/bin/python" ] || [ ! -f "$PIN/hermes_cli/main.py" ]; then
  echo "hermes_pinned.sh: no pinned Hermes at $PIN (run scripts/pin_hermes.sh)" >&2
  exit 127
fi
JOBO_HERMES_PIN_ROOT="$(cd "$PIN" && pwd -P)" exec "$PIN/.venv/bin/python" -I -c 'import os, sys
os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)
sys.path.insert(0, os.environ.pop("JOBO_HERMES_PIN_ROOT"))
sys._hermes_pin_default_home = True
import hermes_bootstrap
if not os.environ.get("HERMES_HOME"):
    from hermes_constants import get_default_hermes_root
    os.environ["HERMES_HOME"] = str(get_default_hermes_root())
from hermes_cli.main import main
sys.argv[0] = "hermes"
sys.exit(main())' "$@"
