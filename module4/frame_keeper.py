"""Keep the apply browser drawing frames while the screen is off (GHO-72).

What happened (run_20261021_2350): Workday's Save and Continue stayed
disabled for good on Reputation (1464, My Information) and Verizon (2219,
Voluntary Disclosures) after one save that failed validation, although the
field was then filled and Workday's own "Errors Found" banner cleared.

Measured on the live Reputation form (scratch browser, nothing saved): a save
with a required field empty disables the button, shows the banner, and
re-enables the button ~1.5 s later from a requestAnimationFrame callback.
With requestAnimationFrame stubbed out the button stays disabled under the
banner exactly as in the run. The apply browser is a headed Chromium on the
user's desktop; overnight the screen is locked and blanked (GNOME ScreenSaver
active, display power-save on), Chromium draws no frames, and no animation
frame callback ever runs: 0 frames in 2-3 s on the live 9333 tabs on
2026-10-22, while the pipeline was applying.

`Emulation.setFocusEmulationEnabled` on the page's CDP session brings frames
back on that same tab (5 frames in ~80 ms). Emulation lives only as long as the
session that set it, so this module holds one browser-level CDP connection for
the whole apply stage and keeps every page target attached with focus
emulation on, including tabs the agent opens later. It never navigates,
evaluates scripts or sends input.

JOBO_FRAME_KEEPER=0 turns it off.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

try:
    import websocket  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

RESCAN_SEC = 10.0
RECONNECT_SEC = 5.0


def enabled() -> bool:
    return os.environ.get("JOBO_FRAME_KEEPER", "1").strip() != "0"


class FrameKeeper:
    """One daemon thread per browser: every page target gets focus emulation."""

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url.rstrip("/")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self._id = 0
        self._lock = threading.Lock()
        # targetId -> sessionId for pages we hold; pending attach call ids.
        self.sessions: dict[str, str] = {}
        self._attaching: dict[int, str] = {}
        self.emulated: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jobo-frame-keeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        t = self._thread
        if t is not None and t.ident is not None and t is not threading.current_thread():
            t.join(timeout=3)

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- CDP plumbing ------------------------------------------------------

    def _send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        with self._lock:
            self._id += 1
            msg = {"id": self._id, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            self._ws.send(json.dumps(msg))
            return self._id

    def _attach(self, info: dict) -> None:
        tid = info.get("targetId", "")
        if info.get("type") != "page" or not tid or tid in self.sessions or tid in self._attaching.values():
            return
        self._attaching[self._send("Target.attachToTarget", {"targetId": tid, "flatten": True})] = tid

    def _handle(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if "id" in msg and msg["id"] in self._attaching:
            tid = self._attaching.pop(msg["id"])
            sid = (msg.get("result") or {}).get("sessionId")
            if sid:
                self.sessions[tid] = sid
                self._send("Emulation.setFocusEmulationEnabled", {"enabled": True}, session=sid)
                self.emulated.add(tid)
        elif method == "Target.targetCreated":
            self._attach(params.get("targetInfo") or {})
        elif method == "Target.detachedFromTarget":
            tid = params.get("targetId") or next(
                (t for t, s in self.sessions.items() if s == params.get("sessionId")), "")
            self.sessions.pop(tid, None)
            self.emulated.discard(tid)
        elif method == "Target.targetDestroyed":
            self.sessions.pop(params.get("targetId", ""), None)
            self.emulated.discard(params.get("targetId", ""))

    def _connect(self) -> None:
        with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=5) as r:
            ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
        self._ws = websocket.create_connection(ws_url, timeout=1.0, suppress_origin=True)
        self.sessions.clear()
        self._attaching.clear()
        self.emulated.clear()
        self._send("Target.setDiscoverTargets", {"discover": True})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect()
                logger.info("frame keeper: holding focus emulation on %s's pages", self.cdp_url)
                last_scan = 0.0
                while not self._stop.is_set():
                    if time.monotonic() - last_scan > RESCAN_SEC:
                        # Belt and braces: a target the discovery events missed.
                        self._send("Target.getTargets")
                        last_scan = time.monotonic()
                    try:
                        msg = json.loads(self._ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    if "id" in msg and "targetInfos" in (msg.get("result") or {}):
                        for info in msg["result"]["targetInfos"]:
                            self._attach(info)
                    else:
                        self._handle(msg)
            except Exception as e:
                if self._stop.is_set():
                    break
                logger.debug("frame keeper: %s; reconnecting", e)
                ws, self._ws = self._ws, None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._stop.wait(RECONNECT_SEC)


# One keeper per worker browser. Workers are threads in one process in some
# tests, so each worker only ever touches the keeper for its own CDP URL.
_KEEPERS: dict[str, FrameKeeper] = {}
_KEEPERS_LOCK = threading.Lock()


def ensure(cdp_url: str) -> FrameKeeper | None:
    """Keep frames coming on this browser; idempotent per CDP URL."""
    if not enabled() or websocket is None or not cdp_url:
        return None
    key = cdp_url.rstrip("/")
    with _KEEPERS_LOCK:
        keeper = _KEEPERS.get(key)
        if keeper is None:
            keeper = _KEEPERS[key] = FrameKeeper(key)
        keeper.start()  # no-op while its thread is alive
        return keeper


def stop(cdp_url: str | None = None) -> None:
    """Stop the keeper for this browser, or every keeper when no URL is given."""
    with _KEEPERS_LOCK:
        if cdp_url is None:
            keepers = list(_KEEPERS.values())
            _KEEPERS.clear()
        else:
            k = _KEEPERS.pop(cdp_url.rstrip("/"), None)
            keepers = [k] if k else []
    for k in keepers:
        k.stop()
