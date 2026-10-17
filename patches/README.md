# Carried Hermes browser fix

The application driver uses the installed Hermes CLI. The browser fixes needed
for the verified run are in this reviewed local Hermes commit:

- Base: `b6b53c69a6ed49cb099cf1bfe76b5e6edd718e5a`
- Carried commit: `ff3dfa6964e6302c9d88a45acafe25b3eeb8260e`
- Patch: `0001-fix-browser-preserve-exact-page-ownership-across-ext.patch`
- Patch SHA-256: `beaed502e82998ad738b3bf2ceb8784d1aac761f48ba5c3886c31c8858e62dd4`

It is installed in the maintainer's local Hermes checkout and is NOT published
to upstream.
This copy prevents migration/restore from silently losing the dependency.

Before applying elsewhere, inspect the installed `hermes --version` and Git
history. If the fix is already carried or upstream has incorporated it, do not
apply it again. On a clean compatible checkout, `git am` can apply this patch;
a different upstream base needs review/rebase and the tests below, not blind
application. Do not overwrite local edits or restart unrelated gateways.

Verify with the canonical Hermes test runner and the included real-browser
`tests/tools/browser_page_binding_live.py` harness. Jobo's
`scripts/test_pipeline_controls_e2e.py` verifies the installed CLI through a
complete model/tool/server flow (opt-in, not part of the default suite; its
docstring says how to run it).
