#!/usr/bin/env python3
"""Fail the regression suite if the jobo Hermes profile is not pinned to Sonnet.

16,193 apply calls have gone to claude-sonnet-5[1m], zero to Opus. The pin
(empty fallback_providers, model.default fixed) was added after the
2026-09-26 incident where Hermes silently fell back to an expired free model
and 101 jobs failed in a row. This test reads the REAL on-disk profile
config - not a fixture - via JOBO_HOST_HOME (the sandbox otherwise points
HOME at a disposable dir; see run_isolated_regressions.py environment()).
A `hermes update` or profile re-clone that moves the model or repopulates
fallback_providers must fail this suite, not just the next live apply run.

Run: uv run python scripts/test_model_guard.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4.model_guard import (  # noqa: E402
    check_profile,
    parse_fallback_providers,
    parse_model_default,
    profile_config_path,
)

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:60s} {note}")


SAMPLE_CONFIG = """\
model:
  default: claude-sonnet-5[1m]
  provider: claude-subscription-directsdk-experimental
  base_url: process://claude-subscription-directsdk-experimental
  api_mode: chat_completions
fallback_providers: []
toolsets:
  - hermes-cli
"""

DRIFTED_MODEL = SAMPLE_CONFIG.replace("claude-sonnet-5[1m]", "claude-opus-5")
REPOPULATED_FALLBACK = SAMPLE_CONFIG.replace(
    "fallback_providers: []", "fallback_providers:\n  - openrouter\n"
)


def main() -> int:
    print("1. Parser correctness on a realistic config shape")
    check("parses model.default", parse_model_default(SAMPLE_CONFIG) == "claude-sonnet-5[1m]")
    check("parses empty fallback_providers", parse_fallback_providers(SAMPLE_CONFIG) == [])
    check(
        "parses a repopulated fallback_providers list",
        parse_fallback_providers(REPOPULATED_FALLBACK) == ["openrouter"],
    )

    print("\n2. check_profile() verdicts")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        healthy = Path(td) / "config.yaml"
        healthy.write_text(SAMPLE_CONFIG)
        check("healthy profile has no violations", check_profile(healthy) == [])

        drifted = Path(td) / "drifted.yaml"
        drifted.write_text(DRIFTED_MODEL)
        v = check_profile(drifted)
        check("non-Sonnet model.default is flagged", len(v) == 1 and "claude-opus-5" in v[0])

        refallback = Path(td) / "refallback.yaml"
        refallback.write_text(REPOPULATED_FALLBACK)
        v = check_profile(refallback)
        check("repopulated fallback_providers is flagged", len(v) == 1 and "openrouter" in v[0])

        missing = Path(td) / "does-not-exist.yaml"
        v = check_profile(missing)
        check("missing profile config is flagged, not silently skipped", len(v) == 1)

    print("\n3. THE REAL DEPLOYED PROFILE (not a fixture)")
    host_home = os.environ.get("JOBO_HOST_HOME")
    real_path = profile_config_path(Path(host_home) if host_home else None)
    real_violations = check_profile(real_path)
    check(
        f"real profile at {real_path} is pinned to Sonnet with no fallback",
        real_violations == [],
        "; ".join(real_violations),
    )

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nFailures:")
        for label, ok, note in results:
            if not ok:
                print(f"  {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
