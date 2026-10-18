#!/usr/bin/env python3
"""Test scripts/check_directsdk_title.py (GHO-10, GHO-53) on stub plugins and stub claude CLIs.

Never touches the real ~/.hermes plugin or the real claude: JOBO_DIRECTSDK_PLUGIN_DIR points at a
temp plugin and JOBO_CLAUDE_BIN at a stub that POSTs to ANTHROPIC_BASE_URL like Claude Code does,
sending the session-title request first unless told not to.

GHO-53: plugin 0.3.1 fixed the title race upstream (CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 on every
child), the old check only knew jobo's own relay patch, and every run was marked FAILED.

Not in run_isolated_regressions.py: it spawns processes, which that harness refuses; the full
run of scripts/run_regression_tests.sh runs it.

Run: python3 scripts/test_directsdk_title_check.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_directsdk_title.py"

SETUP = '''
ALWAYS_QUIET = {"DISABLE_AUTOUPDATER": "1", "DISABLE_FEEDBACK_COMMAND": "1"%s}
def _resolve(command, env):
    return None
def apply_traffic_policy(env, telemetry=None):
    env.update(ALWAYS_QUIET)
    return env
'''
FIXED_SETUP = SETUP % ', "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"'
UNFIXED_SETUP = SETUP % ""
DIRECTSDK = "def spawn(env):\n    apply_traffic_policy(env)\n"

# mode: honours (title only without the flag), ignores (always sends the title), silent (sends nothing)
CLAUDE = '''#!/usr/bin/env python3
import json, os, urllib.request
MODE = %r
def post(system):
    body = json.dumps({"system": [{"type": "text", "text": system}], "messages": []}).encode()
    request = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true", body,
                                     {"Content-Type": "application/json"})
    urllib.request.urlopen(request).read()
if MODE != "silent":
    if MODE == "ignores" or os.environ.get("CLAUDE_CODE_DISABLE_TERMINAL_TITLE") != "1":
        post("You are naming a coding session")
    post("You are the jobo apply agent.")
print("PONG")
'''

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:62s} {note}")


def plugin(root: Path, name: str, setup: str = FIXED_SETUP, directsdk: str = DIRECTSDK) -> Path:
    path = root / name
    path.mkdir()
    (path / "directsdk_setup.py").write_text(setup)
    (path / "directsdk.py").write_text(directsdk)
    return path


def claude(root: Path, mode: str) -> Path:
    path = root / f"claude-{mode}"
    path.write_text(CLAUDE % mode)
    path.chmod(0o755)
    return path


def run(plugin_dir: Path, claude_bin: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "JOBO_DIRECTSDK_PLUGIN_DIR": str(plugin_dir), "JOBO_CLAUDE_BIN": str(claude_bin)}
    return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def main() -> int:
    print("check_directsdk_title.py")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        honours, ignores, silent = claude(tmp, "honours"), claude(tmp, "ignores"), claude(tmp, "silent")
        fixed = plugin(tmp, "fixed")

        # GHO-53: the upstream fix (no jobo patch in admission.py at all) passes.
        ok = run(fixed, honours)
        check("upstream-fixed plugin + honouring CLI -> exit 0", ok.returncode == 0 and "present" in ok.stdout,
              (ok.stdout + ok.stderr).strip()[:80])
        check("plugin dir left untouched (no __pycache__)", sorted(p.name for p in fixed.iterdir()) == ["directsdk.py", "directsdk_setup.py"])

        control = run(fixed, honours, "--control")
        check("--control still sees the title request -> exit 1", control.returncode == 1 and "naming a coding session" in control.stderr,
              control.stderr.strip()[:80])

        # A later update brings the bug back: each way fails loudly.
        unfixed = run(plugin(tmp, "unfixed", setup=UNFIXED_SETUP), honours)
        check("plugin drops the title flag -> exit 1", unfixed.returncode == 1 and "MISSING" in unfixed.stderr
              and "CLAUDE_CODE_DISABLE_TERMINAL_TITLE" in unfixed.stderr, unfixed.stderr.strip()[:80])

        unwired = run(plugin(tmp, "unwired", directsdk="def spawn(env):\n    pass\n"), honours)
        check("turn spawn no longer applies the policy -> exit 1", unwired.returncode == 1 and "apply_traffic_policy" in unwired.stderr,
              unwired.stderr.strip()[:80])

        ignored = run(fixed, ignores)
        check("CLI ignores the flag (2 requests) -> exit 1", ignored.returncode == 1 and "2 requests" in ignored.stderr,
              ignored.stderr.strip()[:80])

        blind = run(fixed, silent)
        check("CLI sends nothing (probe blind) -> exit 1", blind.returncode == 1 and "no request" in blind.stderr,
              blind.stderr.strip()[:80])

        missing = run(tmp / "nope", honours)
        check("no plugin dir -> exit 1", missing.returncode == 1 and "MISSING" in missing.stderr, missing.stderr.strip()[:80])

        broken = run(plugin(tmp, "broken", setup="raise ImportError('needs hermes')\n"), honours)
        check("plugin that will not import -> exit 1", broken.returncode == 1 and "ImportError" in broken.stderr,
              broken.stderr.strip()[:80])

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
