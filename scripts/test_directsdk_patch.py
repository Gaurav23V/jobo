#!/usr/bin/env python3
"""Test scripts/ensure_directsdk_patch.sh (GHO-10) on throwaway plugin dirs.

Never touches the real ~/.hermes plugin: JOBO_DIRECTSDK_PLUGIN_DIR points the
script at a temp copy built by reverse-applying the patch to its own output.

Not in run_isolated_regressions.py: it has to run bash and patch(1), which
that harness refuses (like scripts/test_pipeline_verdict.sh, run it directly).

Run: uv run python scripts/test_directsdk_patch.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ensure_directsdk_patch.sh"
PATCH = ROOT / "hermes_plugin" / "patches" / "directsdk-foreign-admission.patch"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:60s} {note}")


def run(plugin: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "JOBO_DIRECTSDK_PLUGIN_DIR": str(plugin)}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def unpatched_copy(dest: Path) -> None:
    """Rebuild the pre-patch files from the patch's own context and '-' lines."""
    text = PATCH.read_text()
    for chunk in re.split(r"(?m)^--- ", text)[1:]:
        name = re.match(r"a/(\S+)", chunk).group(1)
        old = []
        for line in chunk.splitlines()[2:]:
            if line.startswith("@@"):
                old.append(None)  # hunk boundary
            elif line.startswith((" ", "-")):
                old.append(line[1:])
        # Hunks only carry context, so the rebuilt file is the hunks' old side
        # with fillers between them; patch matches on context, not line numbers.
        body, first = [], True
        for line in old:
            if line is None:
                if not first:
                    body.append("# filler")
                first = False
            else:
                body.append(line)
        if any(line is not None for line in old):
            path = dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(body) + "\n")


def main() -> int:
    print("ensure_directsdk_patch.sh")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        missing = run(tmp / "nope")
        check("no plugin dir -> exit 1", missing.returncode == 1, missing.stderr.strip()[:60])

        plugin = tmp / "plugin"
        unpatched_copy(plugin)
        first = run(plugin)
        check("unpatched plugin -> patch re-applied", first.returncode == 0 and "re-applied" in first.stdout, first.stdout.strip() + first.stderr.strip()[:80])
        check("admission.py now has foreign_request", "def foreign_request" in (plugin / "admission.py").read_text())
        check("directsdk.py passes system to Admission", "system=system)" in (plugin / "directsdk.py").read_text())

        again = run(plugin)
        check("second run is a no-op", again.returncode == 0 and "present" in again.stdout, again.stdout.strip())

        changed = tmp / "changed"
        changed.mkdir()
        (changed / "admission.py").write_text("class Admission:\n    pass\n")
        bad = run(changed)
        check("diverged plugin -> exit 1, untouched", bad.returncode == 1 and (changed / "admission.py").read_text() == "class Admission:\n    pass\n", bad.stderr.strip()[:60])

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
