"""Lifecycle management for the persistent, CDP-attached Chromium instance
that Module 4 drives via Hermes's native browser_* tools.

Replaces the old agent-browser-CLI-managed daemon. We launch (or reuse) one
headed Chromium process bound to the LinkedIn-signed-in profile directory,
exposing a CDP endpoint that every per-job `hermes chat` subprocess attaches
to via the BROWSER_CDP_URL env var (the same mechanism `/browser connect`
uses interactively). One browser process serves the whole apply run; tabs
are swept after each job so a long run doesn't accumulate dozens of them.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome-stable",
    "google-chrome",
    "brave-browser",
)


def cdp_port() -> int:
    raw = os.environ.get("JOBO_BROWSER_CDP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 9333


def cdp_url() -> str:
    return f"http://127.0.0.1:{cdp_port()}"


def _find_chrome_binary() -> str:
    override = os.environ.get("JOBO_CHROME_BIN", "").strip()
    if override and Path(override).exists():
        return override
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(
        "No Chromium-family browser found on PATH "
        f"(tried {', '.join(_CHROME_CANDIDATES)}). Install one or set JOBO_CHROME_BIN."
    )


def _cdp_alive(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def ensure_browser_running(profile_dir: Path, *, startup_timeout: float = 30.0) -> str:
    """Idempotent: attach to an already-listening CDP port, or launch fresh.

    Returns the CDP URL. Raises RuntimeError if the browser never comes up
    (e.g. missing DISPLAY/XAUTHORITY for headed mode).
    """
    port = cdp_port()
    if _cdp_alive(port):
        logger.info("Browser already running on CDP port %d, reusing.", port)
        return cdp_url()

    binary = _find_chrome_binary()
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore",
        # One cookie store for every launch. Without this, Chromium encrypts
        # cookies with the desktop keyring when it can reach one and with a
        # fixed key when it cannot (cron has no D-Bus). A relaunch from the
        # other kind of environment cannot decrypt the cookies and drops all
        # of them: on 2026-10-14 an unattended relaunch of the LinkedIn
        # browser wiped its LinkedIn and Google sessions. The profile is on
        # the user's own machine either way.
        "--password-store=basic",
    ]
    env = dict(os.environ)
    # DISPLAY/XAUTHORITY are an X11 (Linux) concept. macOS opens headed
    # windows through the logged-in user's window server and never sets
    # DISPLAY, so only enforce it on Linux.
    if sys.platform.startswith("linux") and not env.get("DISPLAY"):
        raise RuntimeError(
            "DISPLAY not set: headed Chromium on Linux needs a GUI session env "
            "(DISPLAY/XAUTHORITY). daily_pipeline.sh must export these before "
            "invoking `jobo apply`."
        )

    logger.info("Launching browser: %s", " ".join(args))
    subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if _cdp_alive(port):
            logger.info("Browser ready on CDP port %d.", port)
            return cdp_url()
        time.sleep(0.5)

    raise RuntimeError(
        f"Browser did not open CDP port {port} within {startup_timeout}s. "
        "Check DISPLAY/XAUTHORITY are valid and the profile dir isn't locked "
        "by another Chrome instance."
    )


def close_extra_tabs(*, keep: int = 1) -> None:
    """Best-effort cleanup: close all but the first `keep` tabs.

    Called between jobs so a long apply run (dozens of jobs over hours)
    doesn't accumulate open tabs and bloat memory. Never raises — a failed
    cleanup just leaves an extra tab around, not worth failing the run over.

    CRITICAL: never let the tab count reach zero. Chromium exits when its last
    page target closes, which takes the CDP endpoint down with it. On the
    2026-09-03 run that killed the browser ~20 minutes into the apply stage
    ("CDP connection refused after closing an unrelated leftover tab") and the
    remaining 36 jobs each failed in ~10s against a dead endpoint. A blank
    guard tab is opened if we ever end up with none.
    """
    port = cdp_port()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("close_extra_tabs: could not list tabs: %s", e)
        return

    pages = [t for t in tabs if t.get("type") == "page"]
    keep = max(1, keep)  # never close everything
    closed: list[str] = []
    for tab in pages[keep:]:
        tab_id = tab.get("id")
        if not tab_id:
            continue
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tab_id}", timeout=3)
            closed.append(tab_id)
        except Exception as e:
            logger.debug("close_extra_tabs: failed to close %s: %s", tab_id, e)

    # A close is asynchronous; the next caller must not see these tabs as
    # live (and pick one of them as the tab to keep). See linkedin_router._wait_gone.
    deadline = time.monotonic() + 5
    while closed and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
                live = {t.get("id") for t in json.loads(resp.read().decode("utf-8"))}
        except Exception:
            break
        if not live.intersection(closed):
            break
        time.sleep(0.2)

    ensure_guard_tab()


def ensure_guard_tab() -> bool:
    """Guarantee at least one page target exists, so Chromium stays alive.

    Returns True if a tab is present (or was created), False if the browser
    could not be reached at all.
    """
    port = cdp_port()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False

    if any(t.get("type") == "page" for t in tabs):
        return True

    logger.warning("No page targets left; opening a blank guard tab to keep the browser alive.")
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/json/new?about:blank", method=method
            )
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            continue
    logger.error("Could not open a guard tab; the browser may exit.")
    return False


def ensure_healthy(profile_dir: Path) -> str | None:
    """Verify the browser is reachable, relaunching it if it died.

    Call this BEFORE EVERY JOB, not once per run. `ensure_browser_running`
    was originally invoked a single time in run_module4, so anything that
    killed Chromium mid-run (a closed last tab, a crash, an OOM) turned every
    remaining job into an instant failure with no recovery path. Returns the
    CDP URL, or None if the browser cannot be brought back.
    """
    port = cdp_port()
    if _cdp_alive(port):
        # Alive, but make sure it cannot be killed by a tab close.
        ensure_guard_tab()
        return cdp_url()

    logger.warning("Browser is not reachable on CDP port %d; relaunching.", port)
    try:
        url = ensure_browser_running(profile_dir)
    except RuntimeError as e:
        logger.error("Could not relaunch the browser: %s", e)
        return None
    ensure_guard_tab()
    logger.info("Browser relaunched successfully.")
    return url
