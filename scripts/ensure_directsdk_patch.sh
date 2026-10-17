#!/usr/bin/env bash
# Keep the local fix to Hermes' Claude provider plugin in place (GHO-10).
#
# Claude Code 2.1.299 also names the session in -p mode: a second request
# ("You are naming a coding session", a {"title"} JSON schema) sent through the
# same base URL as the turn. The plugin's relay admits exactly one request per
# turn, so when the title request won the race its {"title": ...} reply became
# the agent's answer. From 2026-10-14 ~13:00 every apply ended after 1 model
# step as "agent produced no verdict", and runs would have sent 0 applications.
# The patch makes the relay refuse requests that lack Hermes' system prompt.
#
# The plugin is pinned from NousResearch/hermes-official-plugins, so
# `hermes plugins update` replaces the patched files. This re-applies the patch
# when it is missing and the plugin source still matches it; otherwise it exits
# non-zero so the pipeline log says why applies may all come back verdict-less.
#
# Exit: 0 patched (already or now), 1 missing and could not be applied.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="${JOBO_DIRECTSDK_PLUGIN_DIR:-$HOME/.hermes/plugins/claude-subscription-directsdk-experimental}"
PATCH="$ROOT/hermes_plugin/patches/directsdk-foreign-admission.patch"

if [ ! -f "$PLUGIN_DIR/admission.py" ]; then
  echo "directsdk patch: no plugin at $PLUGIN_DIR" >&2
  exit 1
fi
if grep -q '^def foreign_request' "$PLUGIN_DIR/admission.py"; then
  echo "directsdk patch: present"
  exit 0
fi
if patch -d "$PLUGIN_DIR" -p1 --forward --dry-run --silent < "$PATCH" >/dev/null 2>&1 \
   && patch -d "$PLUGIN_DIR" -p1 --forward --silent --no-backup-if-mismatch < "$PATCH" >/dev/null 2>&1; then
  echo "directsdk patch: re-applied to $PLUGIN_DIR"
  exit 0
fi
echo "directsdk patch: MISSING and does not apply cleanly to $PLUGIN_DIR (plugin updated?)." \
     "Apply agents may answer with a session-title JSON; see GHO-10 and $PATCH" >&2
exit 1
