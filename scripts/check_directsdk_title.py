#!/usr/bin/env python3
"""Check that Claude Code's session-title request cannot steal an apply turn (GHO-10, GHO-53).

Claude Code also names the session in -p mode: a second request ("You are naming a coding
session") sent through the same base URL as the turn. Hermes' Claude plugin relays exactly one
request per turn, so when the title request won the race its {"title": ...} reply became the
agent's answer and every apply ended "agent produced no verdict" (GHO-10, 14 Oct). jobo used to
carry a local patch to the plugin's relay for it. Plugin 0.3.1 (18 Oct) fixed it upstream
instead (hermes-official-plugins #116): every child it spawns gets CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1
(directsdk_setup.ALWAYS_QUIET, applied by apply_traffic_policy).

Either side can undo that: a plugin update that drops the flag, or a Claude Code update that
stops honouring it. So this checks both:
  1. the plugin's own apply_traffic_policy() still sets the flag, and the turn spawn calls it;
  2. the installed claude, run with that env against a loopback stub upstream, sends exactly one
     request. Any second request would race the turn for the relay's one admission. The stub
     answers locally: no API key, no login, no usage. Scratch HOME and config dir.

Exit: 0 fixed, 1 not fixed or the probe could not tell. Never edits the plugin.

Run: python3 scripts/check_directsdk_title.py [--control]
  --control  probe without the plugin's env (should report the title request; proves the probe
             still sees the bug on this CLI)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN_DIR = Path(os.environ.get("JOBO_DIRECTSDK_PLUGIN_DIR")
                  or Path.home() / ".hermes/plugins/claude-subscription-directsdk-experimental")
FLAG = "CLAUDE_CODE_DISABLE_TERMINAL_TITLE"
TIMEOUT = 90


def fail(reason: str) -> int:
    print(f"directsdk title fix: MISSING - {reason}. Apply agents may answer with a session-title JSON"
          " and no verdict; see GHO-10 / GHO-53 and scripts/check_directsdk_title.py", file=sys.stderr)
    return 1


def plugin_env() -> tuple[dict[str, str], str | None]:
    """The flags the plugin puts on every native child, and the claude it would spawn, from the plugin's own code."""
    for name in ("directsdk_setup.py", "directsdk.py"):
        if not (PLUGIN_DIR / name).is_file():
            raise RuntimeError(f"no {name} in {PLUGIN_DIR}")
    if "apply_traffic_policy(env)" not in (PLUGIN_DIR / "directsdk.py").read_text():
        raise RuntimeError("directsdk.py no longer calls apply_traffic_policy(env) on the turn spawn")
    sys.dont_write_bytecode = True  # shared plugin dir: leave no __pycache__ behind
    sys.path.insert(0, str(PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location("directsdk_setup", PLUGIN_DIR / "directsdk_setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    # telemetry=True: the quiet-traffic flags stay off, the case where native sends the most.
    resolved = setup._resolve(None, dict(os.environ))
    return setup.apply_traffic_policy({}, telemetry=True), resolved[0] if resolved else None


def probe(claude: str, extra_env: dict[str, str]) -> list[str]:
    """System text heads of every request one `claude -p` turn sends upstream."""
    seen: list[str] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                system = body.get("system")
                texts = [system] if isinstance(system, str) else [
                    b.get("text", "") for b in system or [] if isinstance(b, dict)]
                seen.append(" | ".join(t[:60] for t in texts if t))
            except (ValueError, TypeError, AttributeError):
                seen.append("<unparsed request>")
            usage = {"input_tokens": 1, "output_tokens": 1}
            events = [
                {"type": "message_start", "message": {"id": "msg_probe", "type": "message", "role": "assistant",
                 "model": "claude-sonnet-5-5", "content": [], "stop_reason": None, "usage": usage}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PONG"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage},
                {"type": "message_stop"},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write("".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="jobo-title-probe-") as home:
            env = {
                "HOME": home, "PATH": os.defpath, "USER": os.environ.get("USER", "jobo"),
                "CLAUDE_CONFIG_DIR": str(Path(home) / "config"),
                "ANTHROPIC_API_KEY": "sk-ant-probe-not-a-key",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                # Hermes spawns native as an Agent SDK child; that entrypoint is what sends titles.
                "CLAUDE_CODE_ENTRYPOINT": "sdk-py", "CLAUDE_AGENT_SDK_VERSION": "0.1.0",
                "DISABLE_AUTOUPDATER": "1", **extra_env,
            }
            subprocess.run(
                [claude, "-p", "--model", "sonnet", "--tools", "", "--setting-sources", "",
                 "--system-prompt", "You are the jobo apply agent.", "--no-session-persistence",
                 "--max-turns", "1", "Apply to a backend engineering job at a payments company."],
                env=env, cwd=home, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    return seen


def main(argv: list[str]) -> int:
    control = "--control" in argv
    try:
        flags, claude = plugin_env()
    except Exception as exc:  # any plugin shape we cannot read is a failed check, said loudly
        return fail(f"cannot read the plugin's traffic policy ({type(exc).__name__}: {exc})")
    if flags.get(FLAG) != "1":
        return fail(f"plugin apply_traffic_policy() no longer sets {FLAG}=1")

    claude = os.environ.get("JOBO_CLAUDE_BIN") or claude
    if not claude:
        return fail("the plugin finds no claude CLI to probe")
    try:
        seen = probe(claude, {} if control else flags)
    except subprocess.TimeoutExpired:
        return fail(f"probe: claude did not finish within {TIMEOUT}s")
    if not seen:
        return fail("probe: claude sent no request to the stub upstream, so the check cannot tell")
    if len(seen) > 1:
        extra = "; ".join(s for s in seen)
        return fail(f"probe: one claude turn sent {len(seen)} requests through the relay ({extra[:300]})")
    print(f"directsdk title fix: present (plugin sets {FLAG}=1; claude probe sent 1 request)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
