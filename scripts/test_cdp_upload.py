#!/usr/bin/env python3
"""Empirically test which CDP file-upload strategies actually work.

Why: 9 of 19 apply failures on the 2026-09-01 run were resume uploads dying
with "Could not find node/object with given id" on Ashby, Greenhouse, Rippling,
SmartRecruiters, Avature and Phenom. The suspected cause is that each
browser_cdp tool call gets a FRESH CDP session, so a backendNodeId/objectId
obtained in one call is dead by the next - which is exactly what the current
prompt tells the agent to do (getDocument in one call, setFileInputFiles in
another).

This script tests that hypothesis directly against scripts/cdp_upload_probe.html,
which reproduces the five real-world input shapes those ATS portals use, and
compares two strategies:

  A) SPLIT  - resolve the node in one CDP session, set files in a NEW session
              (mimics the agent's current cross-tool-call pattern)
  B) SINGLE - resolve and set files within ONE session (the proposed fix)

Verification is server-side truth: it reads back element.files[].name from the
page, not whether the CDP call returned without error.

Run: uv run python scripts/test_cdp_upload.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import websocket  # from the playwright/websocket-client dep tree
import urllib.request

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "cdp_upload_probe.html"
UPLOAD_FILE = REPO / "personal.example" / "resume.pdf"
CDP_PORT = 9333

CASES = {
    "plain": "#plain-input",
    "hidden": "#hidden-input",
    "testid": "[data-testid=input-resume]",
}


class CdpSession:
    """One websocket connection to a page target = one CDP session."""

    def __init__(self, ws_url: str):
        # Chromium rejects CDP websocket handshakes carrying a browser-style
        # Origin header unless launched with --remote-allow-origins. Real CDP
        # clients simply omit it, so suppress it here rather than relaunching
        # the shared browser with a laxer flag.
        self.ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def page_target() -> tuple[str, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5) as r:
        tabs = json.loads(r.read().decode())
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("no page target available on the CDP endpoint")
    return pages[0]["id"], pages[0]["webSocketDebuggerUrl"]


def new_tab(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{CDP_PORT}/json/new?{url}", method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            tab = json.loads(r.read().decode())
    except Exception:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/new?{url}", timeout=5) as r:
            tab = json.loads(r.read().decode())
    return tab["id"], tab["webSocketDebuggerUrl"]


def close_tab(tab_id: str):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def read_state(ws_url: str) -> dict:
    s = CdpSession(ws_url)
    try:
        r = s.send(
            "Runtime.evaluate",
            {"expression": "window.__uploadState ? window.__uploadState() : 'null'",
             "returnByValue": True},
        )
        raw = r.get("result", {}).get("result", {}).get("value")
        return json.loads(raw) if raw and raw != "null" else {}
    finally:
        s.close()


def strategy_split(ws_url: str, selector: str) -> tuple[bool, str]:
    """Resolve the node in session 1, set files in session 2 (current pattern)."""
    s1 = CdpSession(ws_url)
    try:
        s1.send("DOM.enable")
        doc = s1.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root = doc.get("result", {}).get("root", {}).get("nodeId")
        q = s1.send("DOM.querySelector", {"nodeId": root, "selector": selector})
        node_id = q.get("result", {}).get("nodeId")
        if not node_id:
            return False, f"querySelector found nothing (err={q.get('error')})"
        desc = s1.send("DOM.describeNode", {"nodeId": node_id})
        backend = desc.get("result", {}).get("node", {}).get("backendNodeId")
        if not backend:
            return False, "no backendNodeId from describeNode"
    finally:
        s1.close()

    # Fresh session, as a separate tool call would get.
    s2 = CdpSession(ws_url)
    try:
        s2.send("DOM.enable")
        r = s2.send(
            "DOM.setFileInputFiles",
            {"files": [str(UPLOAD_FILE)], "backendNodeId": backend},
        )
        if r.get("error"):
            return False, f"setFileInputFiles: {r['error'].get('message')}"
        return True, "ok"
    finally:
        s2.close()


def strategy_single(ws_url: str, selector: str) -> tuple[bool, str]:
    """Resolve AND set files inside ONE session (proposed fix)."""
    s = CdpSession(ws_url)
    try:
        s.send("DOM.enable")
        doc = s.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root = doc.get("result", {}).get("root", {}).get("nodeId")
        q = s.send("DOM.querySelector", {"nodeId": root, "selector": selector})
        node_id = q.get("result", {}).get("nodeId")
        if not node_id:
            return False, f"querySelector found nothing (err={q.get('error')})"
        desc = s.send("DOM.describeNode", {"nodeId": node_id})
        backend = desc.get("result", {}).get("node", {}).get("backendNodeId")
        if not backend:
            return False, "no backendNodeId from describeNode"
        r = s.send(
            "DOM.setFileInputFiles",
            {"files": [str(UPLOAD_FILE)], "backendNodeId": backend},
        )
        if r.get("error"):
            return False, f"setFileInputFiles: {r['error'].get('message')}"
        return True, "ok"
    finally:
        s.close()


def strategy_single_objectid(ws_url: str, selector: str) -> tuple[bool, str]:
    """One session, but resolve via Runtime.evaluate -> objectId.

    Works for shadow-DOM inputs that DOM.querySelector cannot reach.
    """
    s = CdpSession(ws_url)
    try:
        expr = (
            "(() => { const direct = document.querySelector(%r); if (direct) return direct;"
            " const host = document.getElementById('shadow-host');"
            " if (host && host.shadowRoot) return host.shadowRoot.querySelector('input[type=file]');"
            " return null; })()" % selector
        )
        r = s.send("Runtime.evaluate", {"expression": expr})
        obj = r.get("result", {}).get("result", {}).get("objectId")
        if not obj:
            return False, "Runtime.evaluate returned no objectId"
        rn = s.send("DOM.requestNode", {"objectId": obj})
        node_id = rn.get("result", {}).get("nodeId")
        if not node_id:
            return False, f"DOM.requestNode failed: {rn.get('error')}"
        r2 = s.send(
            "DOM.setFileInputFiles", {"files": [str(UPLOAD_FILE)], "nodeId": node_id}
        )
        if r2.get("error"):
            return False, f"setFileInputFiles: {r2['error'].get('message')}"
        return True, "ok"
    finally:
        s.close()


def _walk_for_file_inputs(node: dict, out: list[dict], depth: int = 0) -> None:
    """Recursively collect every <input type=file> in a CDP node tree.

    Walks children, shadowRoots, AND contentDocument, which is what makes a
    single pierce=true getDocument enough to reach inputs that
    DOM.querySelector cannot see (shadow DOM, same-process iframes).
    """
    if not isinstance(node, dict):
        return
    if node.get("nodeName") == "INPUT":
        attrs = node.get("attributes", []) or []
        pairs = dict(zip(attrs[::2], attrs[1::2]))
        if pairs.get("type") == "file":
            out.append(
                {
                    "backendNodeId": node.get("backendNodeId"),
                    "nodeId": node.get("nodeId"),
                    "attrs": pairs,
                }
            )
    for key in ("children", "shadowRoots", "pseudoElements"):
        for child in node.get(key, []) or []:
            _walk_for_file_inputs(child, out, depth + 1)
    content_doc = node.get("contentDocument")
    if content_doc:
        _walk_for_file_inputs(content_doc, out, depth + 1)


def strategy_pierce_walk(ws_url: str, selector: str) -> tuple[bool, str]:
    """ONE session: pierce=true getDocument, walk the tree, set files.

    The proposed fix for shadow-DOM/nested inputs: never use
    DOM.querySelector (which cannot cross a shadow boundary) and never use
    Runtime.evaluate objectIds (which DOM.requestNode rejects on these pages).
    Just take backendNodeId straight out of the pierced tree.
    """
    want_id = selector.lstrip("#") if selector.startswith("#") else None
    s = CdpSession(ws_url)
    try:
        s.send("DOM.enable")
        doc = s.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root = doc.get("result", {}).get("root")
        if not root:
            return False, f"getDocument returned no root ({doc.get('error')})"
        found: list[dict] = []
        _walk_for_file_inputs(root, found)
        if not found:
            return False, "pierce walk found no file inputs at all"
        target = None
        if want_id:
            target = next((f for f in found if f["attrs"].get("id") == want_id), None)
        if target is None:
            target = next(
                (f for f in found if f["attrs"].get("id") not in ("plain-input",)), found[0]
            )
        backend = target.get("backendNodeId")
        if not backend:
            return False, f"input found but no backendNodeId: {target['attrs']}"
        r = s.send(
            "DOM.setFileInputFiles",
            {"files": [str(UPLOAD_FILE)], "backendNodeId": backend},
        )
        if r.get("error"):
            return False, f"setFileInputFiles: {r['error'].get('message')}"
        return True, f"ok via pierce walk (found {len(found)} file inputs)"
    finally:
        s.close()


def main() -> int:
    if not UPLOAD_FILE.is_file():
        print(f"FAIL: upload fixture missing: {UPLOAD_FILE}")
        return 1
    print(f"Probe page: {PROBE}")
    print(f"Upload file: {UPLOAD_FILE.name}\n")

    results: dict[str, dict[str, str]] = {}

    for strat_name, strat in (
        ("SPLIT (2 sessions)", strategy_split),
        ("SINGLE (1 session)", strategy_single),
    ):
        print(f"=== Strategy {strat_name} ===")
        results[strat_name] = {}
        for case, selector in CASES.items():
            tab_id, ws_url = new_tab(f"file://{PROBE}")
            time.sleep(1.5)
            try:
                ok, note = strat(ws_url, selector)
                # Server-side truth: did the file actually land on the element?
                state = read_state(ws_url)
                landed = state.get(case) or []
                verified = bool(landed)
                verdict = "PASS" if verified else "FAIL"
                results[strat_name][case] = verdict
                print(
                    f"  {verdict}  {case:8s} call_ok={ok!s:5s} files={landed} "
                    f"{'' if verified else '<- ' + note}"
                )
            finally:
                close_tab(tab_id)

    # Shadow DOM: the one case the probe genuinely reproduces as a failure.
    # Compare the objectId route (what the agent currently tries) against the
    # pierce-walk route (proposed fix).
    print("\n=== Shadow DOM ===")
    for label, strat, case_key in (
        ("objectId route (current)", strategy_single_objectid, "shadow"),
        ("pierce walk (proposed)", strategy_pierce_walk, "shadow"),
    ):
        tab_id, ws_url = new_tab(f"file://{PROBE}")
        time.sleep(1.5)
        try:
            ok, note = strat(ws_url, "#shadow-input")
            state = read_state(ws_url)
            landed = state.get(case_key) or []
            verdict = "PASS" if landed else "FAIL"
            print(
                f"  {verdict}  {label:26s} call_ok={ok!s:5s} files={landed} "
                f"{'' if landed else '<- ' + note}"
            )
            results.setdefault("SHADOW", {})[label] = verdict
        finally:
            close_tab(tab_id)

    # Same-origin iframe: querySelector cannot reach into it either.
    print("\n=== Same-origin iframe ===")
    tab_id, ws_url = new_tab(f"file://{PROBE}")
    time.sleep(1.5)
    try:
        ok, note = strategy_pierce_walk(ws_url, "#iframe-input")
        state = read_state(ws_url)
        landed = state.get("iframe") or []
        verdict = "PASS" if landed else "FAIL"
        print(
            f"  {verdict}  pierce walk               call_ok={ok!s:5s} files={landed} "
            f"{'' if landed else '<- ' + note}"
        )
        results.setdefault("IFRAME", {})["pierce walk"] = verdict
    finally:
        close_tab(tab_id)

    print("\n--- Summary ---")
    for strat, cases in results.items():
        passes = sum(1 for v in cases.values() if v == "PASS")
        print(f"  {strat:22s} {passes}/{len(cases)} passed  {cases}")

    split = results.get("SPLIT (2 sessions)", {})
    single = results.get("SINGLE (1 session)", {})
    shadow = results.get("SHADOW", {})
    split_pass = sum(1 for v in split.values() if v == "PASS")
    single_pass = sum(1 for v in single.values() if v == "PASS")
    print()
    print("CONCLUSIONS:")
    if split_pass == single_pass == len(CASES):
        print(
            f"  * Handle invalidation across CDP sessions is NOT reproducible "
            f"(split={split_pass}/{len(CASES)}, single={single_pass}/{len(CASES)}). "
            "For ordinary inputs - visible, hidden, attribute-selected - the "
            "existing split-call recipe is fine. Do NOT rewrite the prompt "
            "around a one-call rule on this evidence."
        )
    if shadow.get("objectId route (current)") == "FAIL" and (
        shadow.get("pierce walk (proposed)") == "PASS"
    ):
        print(
            "  * Shadow DOM IS a real, reproducible failure of the current "
            "recipe, and the pierce-walk route fixes it. This is a prompt "
            "change worth making."
        )
    elif shadow.get("pierce walk (proposed)") == "FAIL":
        print("  * Shadow DOM still fails even with the pierce walk - needs more work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
