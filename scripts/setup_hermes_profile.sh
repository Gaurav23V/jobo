#!/usr/bin/env bash
# Create/refresh the dedicated `jobo` Hermes profile the apply agent runs in.
#
# Why a separate profile: the apply agent needs settings that would be wrong
# for the user's everyday Hermes (no fallback to other models, aggressive trimming of
# old tool results, the jobo-browser snapshot plugin), and it must not carry
# the user's personal persona/memory into every job run. A profile keeps all of that
# isolated: `hermes -p jobo ...` reads ~/.hermes/profiles/jobo/ only.
#
# Idempotent: safe to re-run after `hermes update` or a config change.
set -euo pipefail

PROFILE="${JOBO_HERMES_PROFILE:-jobo}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_ROOT="${HOME}/.hermes"
PROFILE_DIR="${HERMES_ROOT}/profiles/${PROFILE}"
PROVIDER_PLUGIN="claude-subscription-directsdk-experimental"

if [[ ! -d "$PROFILE_DIR" ]]; then
  echo "Creating Hermes profile '${PROFILE}' (clone of the default profile)..."
  hermes profile create "$PROFILE" --clone --no-alias \
    --description "Jobo job-application agent: browser-only, lean context, no persona or memory."
fi

# Plugins are per profile and a clone does not copy them. Symlink rather than
# copy: the provider plugin stays in step with the default profile, and the
# jobo plugin stays in step with this repo.
mkdir -p "${PROFILE_DIR}/plugins"
ln -sfn "${HERMES_ROOT}/plugins/${PROVIDER_PLUGIN}" "${PROFILE_DIR}/plugins/${PROVIDER_PLUGIN}"
ln -sfn "${ROOT}/hermes_plugin/jobo-browser" "${PROFILE_DIR}/plugins/jobo-browser"
# Local relay fix the provider plugin needs until upstream has it (GHO-10).
bash "${ROOT}/scripts/ensure_directsdk_patch.sh"

set_cfg() { hermes -p "$PROFILE" config set "$1" "$2" >/dev/null; echo "  ${1} = ${2}"; }
echo "Applying jobo profile settings:"
# Only the model login plugin and the snapshot filter; no gateway/observer plugins.
set_cfg plugins.enabled "[${PROVIDER_PLUGIN}, jobo-browser]"
# No fallback models. On 2026-09-26 Claude hit its session limit and Hermes
# fell back to a "free" Nous model whose free period had ended: 101 jobs
# failed in a row. A weaker substitute model filling real applications is not
# wanted either; jobo stops the run instead (module4/runner.py).
set_cfg fallback_providers "[]"
# Replace old tool results (earlier page snapshots) with one-line summaries
# once a job's context passes ~20k tokens. Every model call re-sends the whole
# history, so a snapshot left in place is paid for on every later step.
set_cfg compression.proactive_prune_tokens 20000
set_cfg compression.proactive_prune_min_result_chars 1200
set_cfg compression.proactive_prune_min_reclaim_tokens 3000
set_cfg compression.protect_last_n 6

echo "Profile '${PROFILE}' ready at ${PROFILE_DIR}"
