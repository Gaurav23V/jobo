"""Deterministic file upload for Module 4, performed driver-side over CDP.

Why this exists
---------------
Resume upload is the single largest apply-failure cause: 9 of 19 failures on
2026-09-01 and 15 of 28 on 2026-09-02. On 2026-09-02 the correct CDP technique
was established by experiment (scripts/test_cdp_upload.py) and written into
`module4/prompts.py`. It did not help: on the very next run job393 - the exact
SmartRecruiters shadow-DOM job the experiment was built from - failed again,
and its transcript shows the agent reached for `Runtime.evaluate` -> objectId
-> `DOM.requestNode` anyway, with a single `DOM.getDocument` call and no
shadowRoots walk.

The lesson is that a mechanical, fully-specified DOM operation should not be
delegated to an LLM at all. Prompt instructions compete with model habit and
lose. So this module performs the upload in code:

  * one `DOM.getDocument {depth:-1, pierce:true}` per target
  * a real recursive walk over `children` + `shadowRoots` + `contentDocument`
    (this is what reaches SmartRecruiters' `spl-dropzone` shadow input, which
    `DOM.querySelector` cannot see at all)
  * `DOM.setFileInputFiles` with `backendNodeId`
  * a `change`/`input` event dispatch, because React-based forms often ignore
    files set without one
  * verification by reading `element.files[].name` back out of the page, so a
    "success" means the file is actually attached, not merely that the CDP call
    returned 200

It also searches EVERY page target, not just the active tab, which fixes the
second real failure cause: ATS flows that open the form in a popup (Rippling's
ats.rippling.com) left the agent driving the wrong target while every DOM call
looked inexplicably broken.

The agent's role shrinks to naming which field it wants filled
(`NEED_UPLOAD: resume`), which is a judgement call, while the mechanics happen
here. Mirrors the OTP handoff, which works in production for the same reason.
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from module4 import browser_session

logger = logging.getLogger(__name__)

try:  # websocket-client ships with the playwright dependency tree
    import websocket  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - surfaced as a clear error at runtime
    websocket = None  # type: ignore[assignment]

WS_TIMEOUT_SEC = 20
NAV_SETTLE_SEC = 0.4
UPLOAD_BUDGET_SEC = 8.0
UPLOAD_SETTLE_SEC = 2.0
UPLOAD_POLL_SEC = 0.1
_deadline: ContextVar[float | None] = ContextVar('upload_deadline', default=None)
# Set when target discovery reached at least one page during this upload. If it
# never did (CDP unreachable or stalled), the fallbacks have nothing to click and
# must not spend a second budget.
_saw_pages: ContextVar[bool] = ContextVar('upload_saw_pages', default=False)
FALLBACK_BUDGET_SEC = 12.0
# The site's own upload call can outlast UPLOAD_BUDGET_SEC: Shine's resume
# parser answered in 8.1 s (measured 2026-10-14), so job1455 failed twice with
# "site upload still in flight". While such a call is pending, the deadline is
# extended once, to at most this long from when it was first seen.
INFLIGHT_GRACE_SEC = 20.0


def _grant_inflight_grace(in_flight: list[str], granted: list[bool]) -> None:
    deadline = _deadline.get()
    if in_flight and not granted and deadline is not None:
        granted.append(True)
        _deadline.set(max(deadline, time.monotonic() + INFLIGHT_GRACE_SEC))


def _remaining(cap: float = WS_TIMEOUT_SEC) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('upload readiness/verification budget exhausted')
    return min(cap, remaining)

# Attribute hints that identify which upload slot is which. Ordered: the first
# matching group wins, so "cover" is checked before the looser resume terms to
# avoid putting a resume in the cover-letter slot.
FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "cover": ("cover", "coverletter", "cover_letter", "cover-letter", "letter"),
    "resume": ("resume", "cv", "curriculum", "resume_file", "resumefile", "attachment"),
}


@dataclass
class UploadTarget:
    """One <input type=file> found somewhere in the browser."""

    backend_node_id: int
    attrs: dict
    target_id: str
    ws_url: str
    depth: int = 0
    in_shadow: bool = False
    in_frame: bool = False
    # Position of this input's TAB in CDP's target list; 0 is the active page.
    # Used to break ties between identical inputs on duplicate tabs - see
    # find_file_inputs for the failure this prevents.
    tab_rank: int = 0
    document_id: int = 0
    form_id: int = 0
    identity_attrs: dict = field(default_factory=dict)
    # Lower-cased text describing where the input sits: ancestor classes/ids,
    # its <label>, and the nearest button text. Phenom/Taleo (Avocet, Alpineer)
    # render resume and attachment inputs with NO attributes at all; the only
    # thing telling them apart is `div.resume-upload-wrapper` vs
    # `label.file-label "Upload From Device"`.
    context: str = ""
    # Where the input sits OUTSIDE its own shadow root: the chain of shadow
    # hosts / tagged ancestors, and the nearest section heading. Used only to
    # tell otherwise identical inputs apart (tie-break, identity, hints), never
    # for scoring. SmartRecruiters (Fieldfare 1375) renders two byte-identical
    # `<input id="file-input">` inside two `spl-dropzone` shadow roots; the
    # only difference is the host: `spl-dropzone[data-test=apply-with-resume-
    # container]` under "Easy Apply" vs `spl-dropzone[data-test=resume-upload]`
    # under "Resume". `context` stops at the shadow root, so both looked equal.
    where_chain: str = ""
    where_heading: str = ""
    # The id of the input's form. Southridge (2297) renders one form per role,
    # each with a byte-identical `input.cv-input` under the same "Apply"
    # heading; the form id (`#engineer-form`) is the only thing that differs.
    form_label: str = ""
    # Per selector from `_hint_selectors(hint)`: does it match this input or
    # an ancestor of it in the page? Empty when there was no hint.
    hint_hits: tuple = ()

    @property
    def where(self) -> str:
        return " ".join(x for x in (self.where_chain, self.where_heading) if x)

    @property
    def descriptor(self) -> str:
        bits = []
        for key in ("id", "name", "data-testid", "aria-label", "accept", "class"):
            val = self.attrs.get(key)
            if val:
                bits.append(f'{key}="{val[:40]}"')
        loc = []
        if self.in_shadow:
            loc.append("shadow")
        if self.in_frame:
            loc.append("iframe")
        suffix = f" [{'+'.join(loc)}]" if loc else ""
        place = ""
        if self.in_shadow and self.where_chain:
            place += f" inside {self.where_chain.split(' < ')[0][:70]}"
        if self.where_heading:
            place += f' under "{self.where_heading[:40]}"'
        if self.form_label:
            place += f" in form#{self.form_label[:40]}"
        if any(m in self.context for m in _AUTOFILL_MARKS):
            place += " (autofill zone)"
        return (" ".join(bits) or "<no identifying attributes>") + suffix + place

    def score_for(self, want: str) -> int:
        """How well this input matches a requested field kind."""
        blob = " ".join(
            f"{k}={v}" for k, v in self.attrs.items() if k != "style"
        ).lower()
        score = 0
        for kind, hints in FIELD_HINTS.items():
            for hint in hints:
                if hint in blob:
                    score += 10 if kind == want else -6

        # `accept` is the strongest signal available, because it states what
        # the input is FOR. Observed live on 2026-09-03 (job393, Treyco /
        # SmartRecruiters): the resume went into an "Upload profile image"
        # input that accepted only images, because scoring ignored accept
        # entirely. An input that cannot accept our file type must never win.
        accept = (self.attrs.get("accept") or "").lower()
        if accept:
            doc_ok = any(
                tok in accept
                for tok in (".pdf", "pdf", ".doc", ".docx", ".rtf", ".txt", ".resume", "msword")
            )
            image_only = (
                any(tok in accept for tok in (".png", ".jpg", ".jpeg", ".gif", ".bmp", "image/"))
                and not doc_ok
            )
            if image_only:
                score -= 100  # disqualifying: an image slot is never the resume
            elif doc_ok:
                score += 8

        # Aria labels / names that describe a photo rather than a document.
        if any(
            word in blob
            for word in ("photo", "profile image", "profile picture", "avatar", "headshot", "logo")
        ):
            score -= 100

        # Surrounding context is a weaker signal than the input's own
        # attributes, but it is the only one on attribute-less inputs.
        ctx = self.context
        if ctx:
            for kind, hints in FIELD_HINTS.items():
                if kind == "resume":
                    hints = tuple(h for h in hints if h not in ("cv", "attachment"))
                if any(h in ctx for h in hints):
                    score += 5 if kind == want else -3
            if any(w in ctx for w in ("photo", "avatar", "profile picture", "headshot")):
                score -= 50
            # An "Autofill from resume" zone names the resume too, but only
            # parses it into the form; the field's own slot is the one that
            # is submitted. Dover (Kulfi 1999): identical attributes, and the
            # autofill zone's text gave it the higher score.
            if any(m in ctx for m in _AUTOFILL_MARKS):
                score -= 8

        # Multi-file inputs are usually generic attachment slots: fine, but
        # less specific than a dedicated one.
        if "multiple" in self.attrs:
            score -= 1
        return score

    def can_accept(self, filename: str) -> bool:
        """False when the input's `accept` attribute excludes this file type."""
        accept = (self.attrs.get("accept") or "").strip().lower()
        if not accept:
            return True
        ext = Path(filename).suffix.lower()
        if not ext:
            return True
        tokens = [t.strip() for t in accept.split(",") if t.strip()]
        for tok in tokens:
            if tok in ("*", "*/*"):
                return True
            if tok.startswith("."):
                if tok == ext:
                    return True
            elif tok.endswith("/*"):
                # e.g. image/* - only matches if our extension is that family
                family = tok.split("/")[0]
                if family == "application" and ext in (".pdf", ".doc", ".docx"):
                    return True
                if family == "text" and ext in (".txt", ".rtf"):
                    return True
            elif "pdf" in tok and ext == ".pdf":
                return True
            elif "msword" in tok and ext in (".doc", ".docx"):
                return True
        return False


@dataclass
class UploadResult:
    ok: bool
    detail: str = ""
    field_used: str = ""
    candidates: list[str] = field(default_factory=list)
    # Which node actually took the file, so `upload_file` can re-check it after
    # the page settles. A framework still mounting will have replaced it.
    node_id: int = 0
    node_ws: str = ""

    def as_agent_message(self) -> str:
        if self.ok:
            return (
                f"UPLOAD_DONE: {self.detail}\n\n"
                "The file is now attached and verified on the page. Do NOT try to "
                "upload it again and do not re-probe the file input. Do NOT reload "
                "the page or browser_navigate to the form URL again: that throws the "
                "uploaded file away. Continue with the remaining form fields and submit."
            )
        lines = [f"UPLOAD_FAILED: {self.detail}"]
        if self.candidates:
            lines.append("File inputs that WERE found on the page:")
            lines.extend(f"  - {c}" for c in self.candidates[:8])
            lines.append(
                "If one of these is the right slot, retry with "
                "NEED_UPLOAD: <resume|cover> | <a distinguishing attribute value>"
            )
        else:
            lines.append(
                "No file input exists in any open tab. The upload control is "
                "probably behind a button that must be clicked first, or on a "
                "step you have not reached yet. Click through to the upload step, "
                "then ask again. First check that your last click changed the page: "
                "a cookie banner can swallow it (Heron 1648 asked for an upload on an "
                "unchanged page)."
            )
        return "\n".join(lines)


_DIALOG_ID_BASE = 1_000_000  # ids for fire-and-forget dialog answers


class _Cdp:
    """Minimal CDP client over one websocket to a single page target.

    It also watches two event streams while waiting for replies, because the
    upload's real outcome shows up there, not in `input.files`:

    * `Page.javascriptDialogOpening`: Phenom-hosted Oracle/Taleo career sites
      (careers.alpineer.com, careers.avocet.com) answer a resume upload with a
      native `alert("You have Uploaded Resume Successfully.")`. Unanswered, that
      alert freezes the page's JS thread: every later read hangs until it is
      dismissed. We accept it at once and record the text as evidence.
    * `Network.*`: the app's own upload XHR and its HTTP status.
    """

    def __init__(self, ws_url: str):
        if websocket is None:
            raise RuntimeError(
                "websocket-client is not installed; the deterministic upload path "
                "needs it (uv add websocket-client)."
            )
        # Chromium rejects handshakes that carry a browser-style Origin header
        # unless launched with --remote-allow-origins; real CDP clients omit it.
        self.ws = websocket.create_connection(
            ws_url, timeout=_remaining(WS_TIMEOUT_SEC), suppress_origin=True
        )
        self._id = 0
        self._dialog_id = _DIALOG_ID_BASE
        self.dialogs: list[str] = []
        self.requests: dict[str, dict] = {}
        # Page.fileChooserOpened events seen while interception was on.
        self.choosers: list[dict] = []
        # Frames that are loading a new document (needs Page.enable). Started
        # and stopped events come in pairs, a 204 or cancelled load included.
        self.navigating: set[str] = set()

    def _on_event(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Page.fileChooserOpened":
            self.choosers.append(params)
        elif method == "Page.frameStartedLoading":
            self.navigating.add(params.get("frameId", ""))
        elif method == "Page.frameStoppedLoading":
            self.navigating.discard(params.get("frameId", ""))
        elif method == "Page.javascriptDialogOpening":
            self.dialogs.append(f"{params.get('type', 'dialog')}: {params.get('message', '')}"[:200])
            self._dialog_id += 1
            # Accept, never dismiss: alert() is informational, and a confirm()
            # raised by an upload widget is "replace the existing file?".
            self.ws.send(json.dumps({"id": self._dialog_id, "method": "Page.handleJavaScriptDialog",
                                     "params": {"accept": True}}))
        elif method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            if req.get("method", "GET") != "GET":
                self.requests[params.get("requestId", "")] = {
                    "method": req.get("method"), "url": req.get("url", ""), "status": None, "error": None}
        elif method == "Network.responseReceived":
            rec = self.requests.get(params.get("requestId", ""))
            if rec is not None:
                rec["status"] = (params.get("response") or {}).get("status")
        elif method == "Network.loadingFailed":
            rec = self.requests.get(params.get("requestId", ""))
            if rec is not None:
                rec["error"] = params.get("errorText") or "failed"

    def call(self, method: str, params: dict | None = None) -> dict:
        self.ws.settimeout(_remaining(WS_TIMEOUT_SEC))
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            self.ws.settimeout(_remaining(WS_TIMEOUT_SEC))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg
            if "method" in msg:
                self._on_event(msg)

    def pump(self, seconds: float) -> None:
        """Process events (dialogs, network) for up to `seconds` without a call."""
        end = time.monotonic() + seconds
        while True:
            left = min(end - time.monotonic(), _remaining(WS_TIMEOUT_SEC))
            if left <= 0:
                return
            self.ws.settimeout(left)
            try:
                msg = json.loads(self.ws.recv())
            except (TimeoutError, socket.timeout, getattr(websocket, "WebSocketTimeoutException", TimeoutError)):
                return
            if "method" in msg:
                self._on_event(msg)

    def close(self) -> None:
        try:
            self.ws.close(timeout=0)
        except Exception:  # pragma: no cover - best effort
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=_remaining(timeout)) as resp:
        # urlopen's timeout is an idle timeout, not an overall body deadline.
        # Interrupt even a read consuming a drip-fed/chunked body continuously.
        sock = resp.fp.raw._sock
        expired = threading.Event()
        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(_remaining() if _deadline.get() is not None else timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            chunks = []
            while True:
                sock.settimeout(_remaining(timeout))
                chunk = resp.read1(65536)
                _remaining()
                if expired.is_set():
                    raise TimeoutError('CDP discovery body deadline exhausted')
                if not chunk:
                    if resp.length not in (None, 0):
                        raise OSError('incomplete CDP discovery body')
                    break
                chunks.append(chunk)
                if resp.isclosed():
                    break
            return json.loads(b''.join(chunks).decode('utf-8'))
        finally:
            timer.cancel()
            timer.join()


def list_page_targets(cdp_url: str) -> list[dict]:
    """Every page target, so a popup form is reachable, not just the active tab."""
    try:
        tabs = _http_json(f"{cdp_url.rstrip('/')}/json/list")
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("upload: could not list CDP targets: %s", e)
        return []
    pages = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"]
    browser_session.note_tabs(cdp_url, pages)
    return [t for t in pages if t.get("webSocketDebuggerUrl")]


def list_frame_targets(cdp_url: str) -> list[dict]:
    """Cross-origin iframes (OOPIFs) are separate CDP targets.

    `DOM.getDocument {pierce:true}` on the page does NOT include an
    out-of-process iframe's document. Eastlake's careers page (job 1266) embeds
    its Greenhouse form from job-boards.greenhouse.io, so the resume input lived
    only in the iframe target and every NEED_UPLOAD on that page answered
    "no <input type=file> found in any open tab". The agent then opened the
    embed URL directly to work around it, which is how its reloads began.
    """
    try:
        tabs = _http_json(f"{cdp_url.rstrip('/')}/json/list")
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.debug("upload: could not list CDP frame targets: %s", e)
        return []
    return [t for t in tabs if t.get("type") == "iframe" and t.get("webSocketDebuggerUrl")]


def _attrs_to_dict(attrs: list) -> dict:
    return dict(zip(attrs[::2], attrs[1::2])) if attrs else {}


_CHAIN_ATTRS = ("data-test", "data-testid", "data-automation-id", "data-qa")
_CHAIN_LEN = 5


def _chain_entry(node: dict) -> str | None:
    """A stable label for an ancestor worth naming: custom elements (shadow
    hosts such as `spl-dropzone`) and elements carrying a test id."""
    if node.get("nodeType") != 1:
        return None
    name = (node.get("localName") or node.get("nodeName") or "").lower()
    attrs = _attrs_to_dict(node.get("attributes", []) or [])
    keys = [k for k in _CHAIN_ATTRS if attrs.get(k)]
    if "-" not in name and not keys:
        return None
    return name + "".join(f"[{k}={attrs[k][:48]}]" for k in keys)


def _walk(node: dict, out: list, *, depth: int, in_shadow: bool, in_frame: bool,
          document_id: int = 0, form_id: int = 0, chain: tuple = ()) -> None:
    """Collect file inputs, recursing through shadow roots and frame documents.

    This walk is the whole point of the module: DOM.querySelector cannot cross
    a shadow boundary, and the objectId route (Runtime.evaluate ->
    DOM.requestNode) fails outright on shadow-DOM inputs. Reading
    backendNodeId out of a pierced tree works for both.

    `chain` records the named ancestors (nearest first) INCLUDING shadow hosts,
    because two inputs in identical web components differ only there.
    """
    if not isinstance(node, dict):
        return
    if node.get('nodeName') == '#document':
        document_id = node.get('backendNodeId', 0)
        form_id = 0
        chain = ()
    if node.get('nodeName') == 'FORM':
        form_id = node.get('backendNodeId', 0)
    if node.get("nodeName") == "INPUT":
        attrs = _attrs_to_dict(node.get("attributes", []) or [])
        if (attrs.get("type") or "").lower() == "file":
            backend = node.get("backendNodeId")
            if backend:
                out.append(
                    {
                        "backendNodeId": backend,
                        "attrs": attrs,
                        "depth": depth,
                        "in_shadow": in_shadow,
                        "in_frame": in_frame,
                        "document_id": document_id,
                        "form_id": form_id,
                        "chain": " < ".join(chain[:_CHAIN_LEN]),
                    }
                )
    entry = _chain_entry(node)
    below = ((entry,) + chain)[:_CHAIN_LEN] if entry else chain
    for child in node.get("children", []) or []:
        _walk(child, out, depth=depth + 1, in_shadow=in_shadow, in_frame=in_frame,
              document_id=document_id, form_id=form_id, chain=below)
    for root in node.get("shadowRoots", []) or []:
        _walk(root, out, depth=depth + 1, in_shadow=True, in_frame=in_frame,
              document_id=document_id, form_id=form_id, chain=below)
    content = node.get("contentDocument")
    if content:
        _walk(content, out, depth=depth + 1, in_shadow=in_shadow, in_frame=True)


def find_file_inputs(cdp_url: str, hint: str = "") -> list[UploadTarget]:
    """Find every file input across every open page target.

    Scanning EVERY target is deliberate and load-bearing: popup-based apply
    flows (Rippling opens ats.rippling.com in a new tab) put the real form
    somewhere other than the frontmost page, and a single-tab scan misses it
    entirely.

    But the results must stay ORDERED BY TAB, frontmost first. CDP lists the
    active target first, and `upload_file` ranks purely on how well an input
    matches - so with two copies of the same form open, a background tab's
    `data-testid="input-resume"` ties with the live one and can win. That is
    the Westbay/Rippling failure from 2026-09-11: the live page had 2 file
    inputs, this function returned 8 (duplicates from stale tabs), and
    `DOM.setFileInputFiles` succeeded against an input on a tab nobody was
    looking at. The call reports no error, the node is perfectly connected, and
    the form the agent is filling stays empty - which is why a detached-node
    check does not catch it either.

    `tab_rank` records the tab's position so ranking can prefer the active
    page and only fall back to background tabs when the active one has no
    usable input.

    With a `hint`, each input also records which of the hint's CSS selectors
    match it in the page (`hint_hits`, see `_select`).
    """
    found: list[UploadTarget] = []
    selectors = _hint_selectors(hint)
    pages = list_page_targets(cdp_url)
    if pages:
        _saw_pages.set(True)
    rank_of = {t.get("id", ""): i for i, t in enumerate(pages)}
    frames = list_frame_targets(cdp_url) if pages else []
    parent_of = {f.get("id", ""): f.get("parentId", "") for f in frames}

    def frame_rank(fid: str) -> int:
        # An OOPIF ranks with the tab that hosts it (parents can nest).
        seen = set()
        while fid and fid not in rank_of and fid not in seen:
            seen.add(fid)
            fid = parent_of.get(fid, "")
        return rank_of.get(fid, len(pages))

    scan = [(i, t, False) for i, t in enumerate(pages)]
    scan += [(frame_rank(f.get("id", "")), f, True) for f in frames]
    for tab_rank, tab, is_oopif in scan:
        ws_url = tab["webSocketDebuggerUrl"]
        tid = tab.get("id", "")
        try:
            with _Cdp(ws_url) as cdp:
                cdp.call("DOM.enable")
                doc = cdp.call("DOM.getDocument", {"depth": -1, "pierce": True})
                root = doc.get("result", {}).get("root")
                if not root:
                    continue
                raw: list[dict] = []
                _walk(root, raw, depth=0, in_shadow=False, in_frame=False)
                for r in raw:
                    owner = _form_owner(cdp, r['backendNodeId'])
                    if owner is None:
                        continue  # Snapshot candidate remounted before its owner read.
                    context = _context_of(cdp, r['backendNodeId'])
                    found.append(
                        UploadTarget(
                            backend_node_id=r["backendNodeId"],
                            attrs=r["attrs"],
                            target_id=tid,
                            ws_url=ws_url,
                            depth=r["depth"],
                            in_shadow=r["in_shadow"],
                            in_frame=r["in_frame"] or is_oopif,
                            tab_rank=tab_rank,
                            document_id=r['document_id'],
                            form_id=owner,
                            context=context,
                            where_chain=r.get("chain", ""),
                            where_heading=_heading_of(cdp, r['backendNodeId']),
                            form_label=_call_on_node(cdp, r['backendNodeId'], _FORM_ID_JS)[:80],
                            hint_hits=_selector_hits(cdp, r['backendNodeId'], selectors),
                        )
                    )
        except Exception as e:
            logger.debug("upload: target %s not inspectable: %s", tid[:12], e)
            continue
    return found


_CONTEXT_JS = r"""function(){
  const bits = [];
  let n = this.parentElement;
  for (let k = 0; k < 3 && n; k++, n = n.parentElement) {
    bits.push(String(n.id || ''), String(n.className && n.className.baseVal !== undefined ? n.className.baseVal : n.className || ''));
    // Dover (Kulfi 1999) wraps each input in its own role=button dropzone;
    // its text ("Autofill from resume ...") is all that names the zone.
    if (n.tagName === 'LABEL' || n.tagName === 'BUTTON' || n.getAttribute('role') === 'button')
      bits.push((n.innerText || '').slice(0, 80));
  }
  if (this.labels) for (const l of this.labels) bits.push((l.innerText || '').slice(0, 80));
  const sib = this.parentElement && this.parentElement.querySelector('button, [role=button]');
  if (sib) bits.push((sib.innerText || sib.getAttribute('aria-label') || '').slice(0, 60));
  const r = this.getBoundingClientRect();
  const vis = r.width > 0 && r.height > 0 ? ' __rendered__' : '';
  return bits.join(' ').toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 400) + vis;
}"""


def _context_of(cdp: _Cdp, backend_node_id: int) -> str:
    """Nearby text/classes for an input; empty on any failure (scoring hint only)."""
    return _call_on_node(cdp, backend_node_id, _CONTEXT_JS)


# The nearest section heading ABOVE the input, crossing shadow boundaries
# (input -> its shadow root's host -> ...). Walks previous siblings at each
# level, so `<title>Resume</title><field><dropzone>` yields "Resume" and the
# Easy Apply dropzone yields "Easy Apply".
_HEADING_JS = r"""function(){
  const isHeading = (el) => {
    const tag = el.tagName || '';
    if (/^H[1-6]$/.test(tag) || tag === 'LEGEND') return true;
    if ((el.getAttribute('role') || '') === 'heading') return true;
    if (tag.includes('-') && /TITLE|HEADING|HEADER/.test(tag)) return true;
    return /(^|[-_])(section-)?(title|heading)$/i.test(el.getAttribute('data-test') || el.getAttribute('data-testid') || '');
  };
  const text = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  let n = this;
  for (let k = 0; k < 14 && n; k++) {
    for (let c = n.previousElementSibling; c; c = c.previousElementSibling) {
      if (isHeading(c)) return text(c).slice(0, 80);
    }
    n = n.parentElement || (n.parentNode && n.parentNode.host) || null;
  }
  // No real heading: a short plain-text field label just above the input.
  // Dover (Kulfi 1999) labels its required upload with `<div>Resume *</div>`
  // beside the dropzone, while its autofill dropzone has no label at all.
  const control = 'input, button, select, textarea, [role=button]';
  n = this;
  for (let k = 0; k < 3 && n; k++, n = n.parentElement) {
    for (let c = n.previousElementSibling; c; c = c.previousElementSibling) {
      if (c.matches(control) || c.querySelector(control)) continue;
      const t = text(c);
      if (t && t.length <= 60) return t;
    }
  }
  return '';
}"""


def _heading_of(cdp: _Cdp, backend_node_id: int) -> str:
    return _call_on_node(cdp, backend_node_id, _HEADING_JS)


_FORM_ID_JS = r"""function(){
  const f = this.form || this.closest('form');
  return f && f.id ? String(f.id) : '';
}"""

# Does a selector match the input itself or one of its ancestors? Evaluated in
# the input's own root (shadow or document) and the document. 1/0 per
# selector, or -1 when the browser rejects it as CSS.
_SELECTOR_HITS_JS = r"""function(sels){
  const roots = [this.getRootNode()];
  if (roots[0] !== document) roots.push(document);
  return JSON.stringify(sels.map((s) => {
    try {
      for (const r of roots)
        for (const m of r.querySelectorAll(s)) if (m === this || m.contains(this)) return 1;
      return 0;
    } catch (e) { return -1; }
  }));
}"""


def _hint_selectors(hint: str) -> list[str]:
    """CSS selectors to try for a hint: the hint itself, each `#id` / `.class`
    written inside it, and `#token` for each bare id-like word. Southridge
    (2297) hints were `#engineer-form .cv-input` (a real selector),
    `engineer-form` (the id, bare), and on the live retry `inside
    #engineer-form .cv-input` and `under "Apply" in form#engineer-form`
    (selectors inside prose, which the page rejects or matches nothing with)."""
    hint = (hint or "").strip()
    if not hint:
        return []
    sels = [hint]
    sels += re.findall(r"[#.][A-Za-z][\w-]*", hint)
    sels += [f"#{tok}" for tok in re.findall(r"(?<![#.\w-])([A-Za-z][\w]*[-_][\w-]*)", hint)]
    return list(dict.fromkeys(sels))[:8]


def _selector_hits(cdp: _Cdp, backend_node_id: int, selectors: list[str]) -> tuple:
    if not selectors:
        return ()
    raw = _call_on_node(cdp, backend_node_id, _SELECTOR_HITS_JS, [selectors])
    try:
        return tuple(int(x) for x in json.loads(raw))
    except (ValueError, TypeError):
        return ()


def _call_on_node(cdp: _Cdp, backend_node_id: int, js: str, args: list | None = None) -> str:
    """Run a describe-only function on a node; empty string on any failure."""
    try:
        obj = _checked(cdp, 'DOM.resolveNode', {'backendNodeId': backend_node_id})['object']['objectId']
        try:
            res = _checked(cdp, 'Runtime.callFunctionOn', {
                'objectId': obj, 'functionDeclaration': js, 'returnByValue': True,
                'arguments': [{'value': a} for a in (args or [])]})
            return str(res.get('result', {}).get('value') or '')
        finally:
            _checked(cdp, 'Runtime.releaseObject', {'objectId': obj})
    except Exception:
        return ''


def _checked(cdp: _Cdp, method: str, params: dict | None = None) -> dict:
    response = cdp.call(method, params)
    if response.get("error") or response.get("result", {}).get("exceptionDetails"):
        raise RuntimeError(f"{method}: {response}")
    return response.get("result", {})


def _form_owner(cdp: _Cdp, backend_node_id: int) -> int | None:
    """Actual owner: 0 means connected/unowned; None means detached snapshot.

    Read connectivity and ownership in one JS turn: a remounted old input's
    null form must not be confused with a connected input losing its owner.
    """
    obj = _checked(cdp, 'DOM.resolveNode', {'backendNodeId': backend_node_id})['object']['objectId']
    owner = None
    try:
        owner = _checked(cdp, 'Runtime.callFunctionOn', {
            'objectId': obj,
            'functionDeclaration': 'function(){return this.isConnected ? this.form : "DETACHED";}',
        })['result']
        if owner.get('value') == 'DETACHED':
            return None
        if owner.get('subtype') == 'null':
            return 0
        if not owner.get('objectId'):
            raise RuntimeError('actual form ownership inaccessible')
        return _checked(cdp, 'DOM.describeNode', {'objectId': owner['objectId']})['node']['backendNodeId']
    finally:
        for object_id in (obj, (owner or {}).get('objectId')):
            if object_id:
                _checked(cdp, 'Runtime.releaseObject', {'objectId': object_id})


def _verify_attached(cdp: _Cdp, filename: str, backend_node_id: int,
                     *, expected_size: int | None = None,
                     expected_sha256: str | None = None) -> tuple[bool, str]:
    """Exact live-node readback, not durable verification by itself.

    The caller must rediscover the logical slot after settling; an old handle
    can be detached while a replacement legitimately retains its FileList.
    """
    try:
        resolved = _checked(cdp, "DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = resolved.get("object", {}).get("objectId")
        if not object_id:
            raise RuntimeError("could not resolve node")
        response = _checked(cdp, "Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": """async function(hash){
                const files=Array.from(this.files||[]);
                const digests=hash ? await Promise.all(files.map(async f=>
                    Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer())))
                        .map(b=>b.toString(16).padStart(2,'0')).join(''))) : [];
                return JSON.stringify({names:files.map(f=>f.name),sizes:files.map(f=>f.size),
                    digests,connected:this.isConnected,
                    unchanged:files.length===this.files.length && files.every((f,i)=>f===this.files[i])});
            }""",
            "arguments": [{"value": expected_sha256 is not None}],
            "awaitPromise": True,
            "returnByValue": True,
        })
        data = json.loads(response["result"]["value"])
        if not data["connected"]:
            return False, "DETACHED node; not the live upload field"
        names = data["names"]
        matches = (data['unchanged'] and names == [filename]
                   and (expected_size is None or data['sizes'] == [expected_size])
                   and (expected_sha256 is None or data['digests'] == [expected_sha256]))
        return matches, (", ".join(names) or "(none)") + ("; file identity mismatch" if names and not matches else "")
    except Exception as exc:
        return False, f"verification failed: {exc}"


def _recheck_still_attached(ws_url: str, filename: str, backend_node_id: int,
                            *, settle_sec: float = 1.2) -> tuple[bool, str]:
    """Legacy node-scoped diagnostic. Never treats an inaccessible node as success.

    Not used for durable uploads: node identity alone cannot identify remounts.
    Sleeping with an open CDP session is safe; last-tab cleanup caused the old
    browser deaths, not sleeping. Production verification uses a deadline.
    """
    try:
        time.sleep(_remaining(settle_sec))
        with _Cdp(ws_url) as cdp:
            return _verify_attached(cdp, filename, backend_node_id)
    except Exception as exc:
        return False, f"verification failed: {exc}"


def _purpose_ok(target: UploadTarget, want: str) -> bool:
    blob = " ".join(str(v) for k, v in target.attrs.items() if k not in ("style", "accept")).lower()
    other = "cover" if want == "resume" else "resume"
    return not any(h in blob for h in FIELD_HINTS[other] if h != "attachment") and target.score_for(want) > -50


def _hint_matches(target: UploadTarget, hint: str) -> bool:
    """Does the agent's hint pick out this input?

    Agents write hints as CSS-ish selectors (`input#jobo-resume-input`,
    `input under div.resume-upload-wrapper`), never as our internal
    `id=value` blob, so a plain substring test missed every one of them: job
    831 tagged the inputs with ids and still got "ambiguous" four times.
    """
    hint = (hint or "").lower().strip()
    if not hint:
        return False
    blob = " ".join(f"{k}={v}" for k, v in target.attrs.items()).lower()
    if hint in blob:
        return True
    attrs = {k: str(v).lower() for k, v in target.attrs.items()}
    classes = set(attrs.get("class", "").split())
    ctx = target.context
    for kind, name in re.findall(r"([#.])([\w-]+)", hint):
        # `context` holds the ids and classes of the input's three nearest
        # ancestors. TurboHire (Myrtleby 1635) gives the inputs no attributes;
        # the agent found the resume one "inside div#drop-resume" and got
        # "ambiguous" four times because only the input's own id was read.
        if kind == "#" and (attrs.get("id") == name or (len(name) > 3 and name in ctx.split())):
            return True
        if kind == "." and (name in classes or (len(name) > 3 and name in ctx.split())):
            return True
    chain = target.where_chain.lower()
    for key, val in re.findall(r"\[([\w-]+)\s*[*^$~|]?=\s*['\"]?([^'\"\]]+)", hint):
        if key == "type":
            continue  # `[type=file]` matches every candidate; it picks out none.
        if val and val in attrs.get(key, ""):
            return True
        # The ancestor/shadow-host attributes we print in the descriptor
        # ("inside spl-dropzone[data-test=resume-upload]").
        if val and f"[{key}={val}" in chain:
            return True
    # The section heading we print in the descriptor: `under "Resume"`. A
    # required marker is not part of the name: `under "Resume"` is the field
    # labelled "Resume *" (Dover, Kulfi 1999). Quoted text the agent rules
    # out (`not the "Autofill from resume" one`) names nothing to pick.
    quoted = re.findall(r'(?<!not )(?<!not the )"([^"]+)"', hint)
    heading = _bare(target.where_heading)
    if heading and any(_bare(q) == heading for q in quoted):
        return True
    # The dropzone's own text, as the agent read it off the page.
    squash = lambda s: re.sub(r"\s+", "", s)
    zone = squash(target.context)
    return any(len(q) >= 8 and squash(q) in zone for q in quoted)


def _bare(label: str) -> str:
    return label.lower().strip().rstrip("*").strip()


# Words that mark an "upload your resume to autofill the form" zone. On a
# form that also has a dedicated resume slot, that zone is the wrong default.
_AUTOFILL_MARKS = ("easy apply", "easy-apply", "autofill", "auto-fill", "autocomplete",
                   "apply-with", "apply with", "prefill", "quick apply",
                   "import your profile", "import resume", "import from resume")


def _where_score(target: UploadTarget, want: str) -> int:
    """Tie-break ONLY: how clearly the input's surroundings name the slot.

    Never mixed into `score_for`: these words are often shared by every
    upload on the page, so they decide nothing until attributes tie.
    """
    hints = FIELD_HINTS[want]
    other = FIELD_HINTS["cover" if want == "resume" else "resume"]
    heading = target.where_heading.lower()
    where = target.where.lower()
    score = 0
    if any(h in heading for h in hints if h not in ("cv", "attachment")):
        score += 2
    if any(h in heading for h in other if h not in ("attachment",)):
        score -= 2
    if any(h in target.where_chain.lower() for h in hints if h not in ("cv", "attachment")):
        score += 1
    # TurboHire (Myrtleby 1635) names neither zone in a heading or test id:
    # the autofill input sits next to an "Autofill With Resume" button, which
    # only `context` sees.
    if any(m in where or m in target.context for m in _AUTOFILL_MARKS):
        score -= 2
    # The form's own required slot over an optional one.
    if "required" in target.attrs or target.attrs.get("aria-required") == "true":
        score += 1
    return score


def _score(target: UploadTarget, want: str, hint: str, picked: set | None = None) -> int:
    matched = id(target) in picked if picked is not None else _hint_matches(target, hint)
    return target.score_for(want) + (25 if matched else 0)


def _selector_pick(targets: list[UploadTarget]) -> set | None:
    """The inputs the hint's CSS selectors pick out, as ids; None when they
    pick out nothing narrower than "all of them".

    Every selector that matches at least one input must match a pick, so
    `#engineer-form .cv-input` picks the engineer form's input even though
    `.cv-input` alone matches all four. Selectors the page rejects or that
    match nothing are ignored.
    """
    n = max((len(t.hint_hits) for t in targets), default=0)
    picked = {id(t) for t in targets}
    narrowed = False
    for i in range(n):
        hits = {id(t) for t in targets if i < len(t.hint_hits) and t.hint_hits[i] == 1}
        if hits and hits != picked:
            if not hits & picked:
                return None  # the selectors disagree; fall back to attribute hints
            picked &= hits
            narrowed = True
    return picked if narrowed else None


def _is_hidden_copy(t: UploadTarget, targets: list[UploadTarget]) -> bool:
    """A non-rendered input with a rendered, attribute-identical twin.

    Mapleton (1587/1588/1624, talent.hireloop.ai) renders the whole application
    form twice, a display:none mobile copy first and the desktop copy, each a
    `form#dynamic-form` holding `input#resume_field`. `<label for>` resolves to
    the FIRST element with that id, so both "Upload Resume" labels belong to
    the hidden copy and it outscored the visible one: the file would go into a
    form nobody submits. A responsive duplicate is one slot; keep the rendered copy.
    """
    if t.context.endswith("__rendered__"):
        return False
    return any(o is not t and o.target_id == t.target_id and o.document_id == t.document_id
               and o.attrs == t.attrs and o.context.endswith("__rendered__") for o in targets)


def _select(targets: list[UploadTarget], filename: str, want: str, hint: str) -> UploadTarget:
    usable = [t for t in targets if t.can_accept(filename) and _purpose_ok(t, want)]
    picked = _selector_pick(usable)
    # A hidden input the agent named by selector is the one it wants, even with
    # a rendered twin elsewhere (Southridge 2297: four role panels, one input
    # each, the wanted panel possibly still closed).
    eligible = [t for t in usable if not _is_hidden_copy(t, targets)
                or (picked is not None and id(t) in picked
                    and not any(id(o) in picked for o in usable if o.context.endswith("__rendered__")))]
    if not eligible:
        raise RuntimeError("no file input accepts the requested file and purpose")
    score = lambda t: _score(t, want, hint, picked)
    # Preserve all-target popup discovery and historical score/tab ordering.
    eligible.sort(key=lambda t: (-score(t), t.tab_rank, t.depth))
    chosen = eligible[0]
    ties = [t for t in eligible if t.target_id == chosen.target_id and score(t) == score(chosen)]
    if len(ties) > 1:
        # Keka (Northfold 974, measured on salesdemo.keka.com): a visible
        # input#resume-upload.resume-upload plus a hidden twin .resume-upload
        # inside the "upload" link. Both POST to the same resume parser, so
        # they are one logical slot, yet we refused every request as
        # ambiguous. When exactly one of the tied inputs is rendered, the
        # user-facing one is the slot.
        rendered = [t for t in ties if t.context.endswith("__rendered__")]
        if len(rendered) == 1:
            chosen, ties = rendered[0], rendered
    if len(ties) > 1:
        # SmartRecruiters (Fieldfare 1375): two identical shadow inputs, one
        # in the "Easy Apply" autofill dropzone, one in the "Resume" section.
        # Their only difference is outside the shadow root. Pick the one whose
        # section names the wanted slot, if exactly one does best.
        best = max(_where_score(t, want) for t in ties)
        top = [t for t in ties if _where_score(t, want) == best]
        if len(top) == 1:
            chosen, ties = top[0], top
    if len(ties) != 1:
        raise RuntimeError("ambiguous upload slots; supply a distinguishing hint")
    # Freeze the attributes NEEDED to distinguish the original slot. Unique
    # semantic test IDs may survive changing generated IDs, but shared IDs must
    # never lose their original secondary discriminator as siblings disappear.
    key = next((k for k in ('data-testid', 'name', 'aria-label', 'id') if chosen.attrs.get(k)), None)
    if key:
        chosen.identity_attrs = {key: chosen.attrs[key]}
        same = [t for t in targets if t.target_id == chosen.target_id
                and t.document_id == chosen.document_id and t.form_id == chosen.form_id
                and t.attrs.get(key) == chosen.attrs[key]]
        for extra in ('name', 'aria-label', 'id'):
            if len(same) > 1 and chosen.attrs.get(extra):
                chosen.identity_attrs[extra] = chosen.attrs[extra]
                same = [t for t in same if t.attrs.get(extra) == chosen.attrs[extra]]
        if len(same) > 1 and chosen.where_chain:
            # Identical inputs in identical web components (SmartRecruiters'
            # two `input#file-input`): the host chain is the identity.
            chosen.identity_attrs[_WHERE_KEY] = chosen.where_chain
            same = [t for t in same if t.where_chain == chosen.where_chain]
        if len(same) != 1:
            raise RuntimeError('ambiguous original upload identity')
    return chosen


# Pseudo-attribute in `identity_attrs`: the input's named-ancestor chain.
_WHERE_KEY = "__where_chain__"


def _identity_value(attrs: dict, chain: str, key: str):
    return chain if key == _WHERE_KEY else attrs.get(key)


def _live_slot(cdp: _Cdp, original: UploadTarget, filename: str, want: str) -> UploadTarget | None:
    root = _checked(cdp, "DOM.getDocument", {"depth": -1, "pierce": True}).get("root")
    if not root:
        raise RuntimeError("verification inaccessible: missing document")
    raw: list[dict] = []
    _walk(root, raw, depth=0, in_shadow=False, in_frame=False)
    # Verify document/frame and form existence even if the input is temporarily absent.
    ids = set()
    def collect(node):
        ids.add(node.get("backendNodeId"))
        for child in (node.get("children", []) + node.get("shadowRoots", [])):
            collect(child)
        if node.get("contentDocument"):
            collect(node["contentDocument"])
    collect(root)
    if original.document_id not in ids or (original.form_id and original.form_id not in ids):
        raise RuntimeError(_SCOPE_DRIFT)
    candidates = []
    in_form = drifted = False
    for item in raw:
        t = UploadTarget(item["backendNodeId"], item["attrs"], original.target_id,
                         original.ws_url, item["depth"], item["in_shadow"], item["in_frame"],
                         original.tab_rank, item["document_id"], item["form_id"],
                         where_chain=item.get("chain", ""), where_heading=original.where_heading,
                         form_label=original.form_label)
        if t.document_id != original.document_id:
            continue
        if original.identity_attrs:
            if any(_identity_value(t.attrs, t.where_chain, k) != v
                   for k, v in original.identity_attrs.items()):
                continue
        elif t.backend_node_id != original.backend_node_id:
            continue
        owner = _form_owner(cdp, t.backend_node_id)
        if owner is None:
            return None  # Poll a fresh scoped tree within the existing deadline.
        t.form_id = owner
        if t.form_id != original.form_id:
            # Either the slot left its form, or this is the same-named input
            # of ANOTHER form (Hireloop's hidden mobile copy of the whole form).
            # Only the first is drift: decide once the original form is read.
            drifted = True
            continue
        in_form = True
        if t.can_accept(filename) and _purpose_ok(t, want):
            candidates.append(t)
    if drifted and not in_form:
        raise RuntimeError('actual form owner changed; refusing scope drift')
    if len(candidates) > 1:
        raise RuntimeError("ambiguous replacement upload slots in original form")
    return candidates[0] if candidates else None


_SCOPE_DRIFT = "original frame/document/form replaced; refusing scope drift"
_PAGE_JS = "JSON.stringify([location.origin + location.pathname, document.readyState])"


def _page_address(cdp: _Cdp) -> tuple[str, str]:
    raw = _checked(cdp, "Runtime.evaluate", {"expression": _PAGE_JS, "returnByValue": True})
    addr, ready = json.loads(raw["result"]["value"])
    return addr, ready


def _rebind_after_reload(cdp: _Cdp, original: UploadTarget, page_addr: str,
                         filename: str, want: str) -> UploadTarget | None:
    """The same slot on a page that reloaded itself at the same address.

    Hirebridge QuickApply (Humongous 2396, ASP.NET): the file input's
    onchange clicks a hidden "parse" button, whose __doPostBack POSTs the whole
    form, file included, and the server answers with a fresh copy of the same
    page. Every node is new, so `_live_slot` refused it as scope drift four
    times while each request was in fact a resume upload. A new document at
    the same address with exactly one input of the same name/id is the same
    slot; any other replacement (another address, the input gone or doubled,
    a form swapped by script inside the same document) is still refused. None
    while the new page is still loading.
    """
    keys = {k: v for k, v in original.identity_attrs.items() if k in ("name", "id", "data-testid")}
    if not keys or original.in_frame or original.in_shadow:
        raise RuntimeError(_SCOPE_DRIFT)
    addr, ready = _page_address(cdp)
    if addr != page_addr:
        raise RuntimeError(f"the page moved to {addr[:80]} after the write; refusing scope drift")
    if ready != "complete":
        return None
    root = _checked(cdp, "DOM.getDocument", {"depth": -1, "pierce": True}).get("root") or {}
    if root.get("backendNodeId") == original.document_id:
        # Same document, new form: page script swapped it, nothing was reloaded.
        raise RuntimeError(_SCOPE_DRIFT)
    raw: list[dict] = []
    _walk(root, raw, depth=0, in_shadow=False, in_frame=False)
    same = [r for r in raw if not r["in_frame"] and not r["in_shadow"]
            and all(r["attrs"].get(k) == v for k, v in keys.items())]
    if len(same) != 1:
        raise RuntimeError(_SCOPE_DRIFT)
    owner = _form_owner(cdp, same[0]["backendNodeId"])
    if owner is None:
        return None
    new = replace(original, backend_node_id=same[0]["backendNodeId"], attrs=same[0]["attrs"],
                  depth=same[0]["depth"], document_id=same[0]["document_id"], form_id=owner)
    if not (new.can_accept(filename) and _purpose_ok(new, want)):
        raise RuntimeError(_SCOPE_DRIFT)
    return new


# Our file name, anywhere in the page the server sent back: visible text,
# markup (a script variable, a hidden input's value attribute) or a field
# value. The name carries the job id and a content hash, so the page can only
# hold it if the site received the file.
_CARRIES_FILE_JS = r"""function(n){
  const low = n.toLowerCase();
  if ((document.documentElement.outerHTML || '').toLowerCase().includes(low)) return true;
  for (const i of document.querySelectorAll('input, textarea'))
    if (String(i.value || '').toLowerCase().includes(low)) return true;
  return false;
}"""


def _page_carries_file(cdp: _Cdp, filename: str) -> bool:
    doc = _checked(cdp, "Runtime.evaluate", {"expression": "document"})["result"]["objectId"]
    try:
        res = _checked(cdp, "Runtime.callFunctionOn", {
            "objectId": doc, "functionDeclaration": _CARRIES_FILE_JS,
            "arguments": [{"value": filename}], "returnByValue": True})
        return bool(res.get("result", {}).get("value"))
    finally:
        _checked(cdp, "Runtime.releaseObject", {"objectId": doc})


def _attach(cdp: _Cdp, target: UploadTarget, path: Path) -> None:
    """Set the file. Nothing else.

    `DOM.setFileInputFiles` already fires trusted `input` and `change` events
    (measured on careers.alpineer.com: a listener saw `input:true change:true`).
    The old code dispatched a SECOND synthetic pair on top, so every app that
    uploads on `change` uploaded twice in parallel:
      * careers.alpineer.com (Phenom over Oracle): two POST /fileUpload; the page
        showed "Unable to upload the resume. Please choose another one." while
        our FileList check passed (job 721, tonight).
      * jobs.lever.co (Woodgrove 919): two POST /parseResume, the first aborted,
        and the page showed "Couldn't auto-read resume." next to "Success!".
    """
    _checked(cdp, "DOM.setFileInputFiles", {
        "files": [str(path.resolve())], "backendNodeId": target.backend_node_id})


# Text an ATS shows when IT rejected the upload, as opposed to our FileList.
_APP_ERROR_RE = (
    r"unable to upload|upload(?:ing)? failed|failed to upload|could ?n['’o]?t upload|"
    r"error (?:while )?uploading|please choose another|invalid file|file type (?:is )?not "
    r"(?:allowed|supported)|file (?:is )?too large|exceeds the maximum"
)
_APP_STATE_JS = r"""function(fname, errRe){
  // Light DOM text plus every open shadow root: web-component dropzones
  // (SmartRecruiters spl-dropzone) render the file chip inside their shadow.
  // A live SR form has ~1,800 shadow roots (flag icons), so walk them all but
  // keep only leaf text that names the file or matches an error.
  let text = (document.body && document.body.innerText) || '';
  const lowName = fname.toLowerCase();
  const errTest = new RegExp(errRe, 'i');
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        for (const leaf of el.shadowRoot.querySelectorAll('*')) {
          if (leaf.childElementCount) continue;
          const t = (leaf.textContent || '').trim();
          if (t && (t.toLowerCase().includes(lowName) || errTest.test(t))) text += '\n' + t.slice(0, 200);
        }
        visit(el.shadowRoot);
      }
    }
  };
  try { visit(document); } catch (e) {}
  const errs = (text.match(new RegExp('[^\n]*(?:' + errRe + ')[^\n]*', 'gi')) || []).map(s => s.trim().slice(0, 120));
  // Count where the app itself shows the file name (chip, link, label), not the input's value.
  const low = fname.toLowerCase();
  let shown = text.toLowerCase().split(low).length - 1;
  // A chip that cuts a long name short (TurboHire 1635: "Alex_Example_Re..."
  // for <First>_<Last>_Resume_1635_581287.pdf) shows a 12+ character prefix
  // and an ellipsis. _app_evidence compares counts before and after the
  // write, so an old chip that truncates the same way never counts.
  for (const t of text.match(/[\w.\-]{12,}?(?:\.\.\.|…)/g) || []) {
    const p = t.replace(/(?:\.\.\.|…)$/, '').toLowerCase();
    if (p.length >= 12 && p !== low && low.startsWith(p)) shown += 1;
  }
  return JSON.stringify({errs, shown});
}"""
_UPLOADISH_URL = re.compile(r"upload|file|resume|attach|parse|document|cv", re.I)
# Direct-to-storage uploads carry none of those words: Greenhouse POSTs the file
# straight to https://grnhse-prod-....s3.us-west-2.amazonaws.com/ on change.
# Only a write to such a host counts (pages GET images from S3 all the time).
_STORAGE_URL = re.compile(
    r"\.s3[.-][\w.-]*amazonaws\.com|storage\.googleapis\.com|blob\.core\.windows\.net", re.I)
# An _app_evidence line saying the site STORED the file: a 2xx POST/PUT to an
# attachment/upload endpoint. Oracle's resume import (Trey 272, 1429) posts
# .../recruitingCECandidateResumeParsers (a parse: not storage) and then
# .../JobApplicationDrafts/<id>/child/attachments -> 201 (the attachment).
_STORED_EVIDENCE = re.compile(r"^(?:POST|PUT) \S*(?:attach|upload)\S* -> HTTP 2\d\d$", re.I)
_PARSE_ONLY_URL = re.compile(r"pars|autofill|import|extract", re.I)


def _stored(confirmed: list[str]) -> list[str]:
    return [c for c in confirmed if _STORED_EVIDENCE.search(c) and not _PARSE_ONLY_URL.search(c)]


def _app_state(cdp: _Cdp, filename: str) -> dict:
    """The app's visible state: error banners and where it shows our file name."""
    root = _checked(cdp, "Runtime.evaluate", {"expression": "document", "returnByValue": False})
    obj = root.get("result", {}).get("objectId")
    try:
        res = _checked(cdp, "Runtime.callFunctionOn", {
            "objectId": obj, "functionDeclaration": _APP_STATE_JS,
            "arguments": [{"value": filename}, {"value": _APP_ERROR_RE}], "returnByValue": True})
        return json.loads(res["result"]["value"])
    finally:
        if obj:
            _checked(cdp, "Runtime.releaseObject", {"objectId": obj})


def _app_evidence(cdp: _Cdp, before: dict, filename: str, req_mark: set) -> tuple[list[str], list[str]]:
    """(confirmations, rejections) observed from the APP since the write."""
    now = _app_state(cdp, filename)
    ok: list[str] = []
    bad: list[str] = []
    new_errs = [e for e in now["errs"] if e not in before["errs"]]
    bad.extend(f'page says "{e}"' for e in new_errs)
    for rid, rec in cdp.requests.items():
        if rid in req_mark:
            continue
        if not (_UPLOADISH_URL.search(rec["url"]) or
                (rec["method"] in ("POST", "PUT") and _STORAGE_URL.search(rec["url"]))):
            continue
        short = re.sub(r"\?.*$", "", rec["url"])[-60:]
        if rec["status"] is not None and rec["status"] >= 400:
            bad.append(f"{rec['method']} {short} -> HTTP {rec['status']}")
        elif rec["status"] is not None:
            ok.append(f"{rec['method']} {short} -> HTTP {rec['status']}")
        elif rec["error"] and "ABORTED" not in rec["error"]:
            bad.append(f"{rec['method']} {short} -> {rec['error']}")
        elif not rec["error"]:
            ok.append(f"{rec['method']} {short} (in flight)")
    for d in cdp.dialogs:
        (bad if re.search(_APP_ERROR_RE, d, re.I) else ok).append(f'page {d!r}'[:120])
    if now["shown"] > before["shown"]:
        ok.append(f"page now shows {filename!r}")
    return ok, bad


CHOOSER_WAIT_SEC = 2.5

# Upload controls that open the browser's file chooser without any
# <input type=file> in the DOM. SAP SuccessFactors "My Documents" tiles (Litware
# 1316, Lucerne 216) are the shape: a role=button "+" span
# (`<n>:_attachIcon.addAttachments`) whose handler calls .click() on an input
# that is never attached to the document. A DOM walk finds nothing, a trusted
# click "does nothing" visible (the chooser is an OS window the page snapshot
# cannot show), and NEED_UPLOAD answered "no <input type=file> found".
# CDP's Page.setInterceptFileChooserDialog turns that chooser into a
# Page.fileChooserOpened event carrying the input's backendNodeId, which
# DOM.setFileInputFiles accepts even though the node is detached.
_CHOOSER_CONTROLS_JS = r"""function(want, hint){
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const NEVER = /\b(submit|apply|next|continue|save|remove|delete|trash|dropbox|google ?drive|onedrive|linkedin|indeed|manually|paste|sign ?in|log ?in|cancel|close|download|view|print)\b/;
  const UPLOADISH = /upload|attach|browse|choose (a )?file|select (a )?file|add (a )?(document|file)|addattachments|_attachicon|glyphicon-plus|^\+$/;
  const KIND = {resume: /\b(resume|cv|curriculum)\b/, cover: /cover/};
  const other = want === 'cover' ? KIND.resume : KIND.cover;
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const own = el => norm([el.getAttribute('aria-label'), el.getAttribute('title'), el.id,
    typeof el.className === 'string' ? el.className : '', el.innerText || el.textContent || el.value,
    (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => { const n = id && document.getElementById(id); return n ? n.textContent : ''; }).join(' ')].join(' '));
  // Smallest ancestor that names a document kind: the tile this control belongs to.
  // Returns [text, depth]. Depth matters: on Litware the "Supporting
  // Documentation" tile only reaches the word "resume" via the section's
  // instruction text several levels up, while the real Resume/CV tile names it
  // on its own label. The nearer naming wins.
  const slot = el => { for (let n = el, k = 0; n && k < 6; n = n.parentElement, k++) {
    const t = norm(n.innerText || n.textContent); if (t && t.length < 600 && (KIND.resume.test(t) || KIND.cover.test(t))) return [t, k]; } return ['', 9]; };
  const hintToks = norm(hint).split(/[^a-z0-9]+/).filter(t => t.length > 2);
  let found = [];
  for (const el of document.querySelectorAll('button, [role=button], a, [onclick], input[type=button], label')) {
    if (!vis(el) || el.disabled) continue;
    const o = own(el);
    if (NEVER.test(o) || !UPLOADISH.test(o)) continue;
    const [st, depth] = slot(el);
    const s = st || o;
    const mine = KIND[want].test(s) && !(want === 'resume' && KIND.cover.test(s) && !KIND.resume.test(s));
    if (!mine || (other.test(s) && !KIND[want].test(o) && other.test(o))) continue;
    let score = 10 + (/addattachments|_attachicon/.test(o) ? 3 : 0) + (el.getAttribute('role') === 'button' || el.tagName === 'BUTTON' ? 1 : 0);
    score += KIND[want].test(o) ? 6 : 2 * Math.max(0, 6 - depth);
    if (/supporting|additional|other document|references|work example|portfolio/.test(o + ' ' + s.slice(0, 80))) score -= 8;
    score += 5 * hintToks.filter(t => (o + ' ' + s).includes(t)).length;
    found.push({el, score, label: (o.slice(0, 80) + ' | in: ' + s.slice(0, 60))});
  }
  // A wrapper with onclick and the button inside it are ONE control: keep the innermost.
  found = found.filter(f => !found.some(g => g !== f && f.el.contains(g.el)));
  found.sort((a, b) => b.score - a.score);
  window.__joboChooserCtl = found.map(f => f.el);
  return JSON.stringify(found.map(f => ({score: f.score, label: f.label})));
}"""

# Page-dispatched input + change on the file input, once (see _upload_dom).
_RESEND_CHANGE_JS = r"""function(){
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return String(this.files ? this.files.length : 0);
}"""

_CHOOSER_POINT_JS = r"""function(i){
  const el = (window.__joboChooserCtl || [])[i];
  if (!el || !el.isConnected) return JSON.stringify({err: 'control vanished'});
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  const ok = !!hit && (hit === el || el.contains(hit) || hit.contains(el));
  return JSON.stringify({x, y, ok, hit: hit ? (hit.tagName + '#' + hit.id + '.' + String(hit.className).slice(0, 40)) : ''});
}"""


_CLICKED_NO_CHOOSER = UploadResult(False, "clicked the upload button; it opened no file chooser")

# Where the site itself shows our file name inside a slot of the requested kind.
_ALREADY_SHOWN_JS = r"""function(fname, want){
  const low = fname.toLowerCase();
  const KIND = {resume: /\b(resume|cv|curriculum)\b/i, cover: /cover/i};
  const other = want === 'cover' ? KIND.resume : KIND.cover;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    if (!String(t.nodeValue).toLowerCase().includes(low)) continue;
    const el = t.parentElement;
    if (!el || el.closest('script,style,option') || !el.getClientRects().length) continue;
    for (let n = el, k = 0; n && k < 6; n = n.parentElement, k++) {
      const txt = (n.innerText || '').slice(0, 400);
      if (KIND[want].test(txt.replace(fname, ''))) {
        if (other.test(txt) && !KIND[want].test(txt.split(fname)[0].slice(-120))) break;
        return txt.replace(/\s+/g, ' ').trim().slice(0, 80);
      }
    }
  }
  return '';
}"""


def _already_attached(cdp_url: str, path: Path, want: str) -> UploadResult | None:
    """The site already shows this file in a slot of this kind (e.g. a repeat
    request after Greenhouse replaced its input with a file chip)."""
    for tab in _chooser_targets(cdp_url):
        try:
            with _Cdp(tab["webSocketDebuggerUrl"]) as cdp:
                doc = _checked(cdp, "Runtime.evaluate", {"expression": "document"})["result"]["objectId"]
                where = _checked(cdp, "Runtime.callFunctionOn", {
                    "objectId": doc, "functionDeclaration": _ALREADY_SHOWN_JS,
                    "arguments": [{"value": path.name}, {"value": want}], "returnByValue": True})["result"]["value"]
        except Exception:
            continue
        if where:
            # agent_driver keys on the "ALREADY_ATTACHED" prefix to not charge
            # the round against MAX_UPLOAD_ROUNDS.
            return UploadResult(True, f"ALREADY_ATTACHED {path.name}: the site already shows it in "
                                f"{where!r}. Nothing was uploaded again.", where)
    return None


def _chooser_targets(cdp_url: str) -> list[dict]:
    pages = list_page_targets(cdp_url)
    return pages + (list_frame_targets(cdp_url) if pages else [])


def _upload_via_chooser(cdp_url: str, path: Path, want: str, hint: str) -> UploadResult | None:
    """Click the slot's upload control with interception on and answer the chooser.

    Returns None when no page has a matching upload control (caller carries on
    with the DOM path). Never clicks anything that reads as submit/apply/next/
    remove/cloud-import, never clicks when two tiles of the requested kind tie,
    and sends exactly one trusted click per call.
    """
    for tab in _chooser_targets(cdp_url):
        try:
            cdp = _Cdp(tab["webSocketDebuggerUrl"])
        except Exception:
            continue
        with cdp:
            try:
                for m in ("DOM.enable", "Runtime.enable", "Page.enable", "Network.enable"):
                    _checked(cdp, m)
                doc = _checked(cdp, "Runtime.evaluate", {"expression": "document"})["result"]["objectId"]
                res = _checked(cdp, "Runtime.callFunctionOn", {
                    "objectId": doc, "functionDeclaration": _CHOOSER_CONTROLS_JS,
                    "arguments": [{"value": want}, {"value": hint or ""}], "returnByValue": True})
                cands = json.loads(res["result"]["value"])
            except Exception as e:
                logger.debug("upload: chooser scan failed on %s: %s", tab.get("id", "")[:12], e)
                continue
            if not cands:
                continue
            labels = [c["label"] for c in cands]
            if len(cands) > 1 and cands[0]["score"] == cands[1]["score"]:
                return UploadResult(False, "several upload buttons match this document kind; "
                                    "supply a hint naming the right one", candidates=labels)
            pt = json.loads(_checked(cdp, "Runtime.callFunctionOn", {
                "objectId": doc, "functionDeclaration": _CHOOSER_POINT_JS,
                "arguments": [{"value": 0}], "returnByValue": True})["result"]["value"])
            if pt.get("err") or not pt.get("ok"):
                return UploadResult(False, f"upload button {labels[0]!r} is covered or gone "
                                    f"({pt.get('err') or 'hit ' + pt.get('hit', '?')})", candidates=labels)
            before = _app_state(cdp, path.name)
            req_mark = set(cdp.requests)
            _checked(cdp, "Page.setInterceptFileChooserDialog", {"enabled": True})
            try:
                for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                    _checked(cdp, "Input.dispatchMouseEvent", {"type": kind, "x": pt["x"], "y": pt["y"],
                                                                 "button": "left", "clickCount": 1})
                end = time.monotonic() + CHOOSER_WAIT_SEC
                while not cdp.choosers and time.monotonic() < end:
                    cdp.pump(min(0.2, max(0.0, end - time.monotonic())))
                if not cdp.choosers:
                    # The control opened something else (an in-page dialog or a
                    # menu that mounts a real input). Let the DOM path take it.
                    return _CLICKED_NO_CHOOSER
                node = cdp.choosers[0].get("backendNodeId")
                _checked(cdp, "DOM.setFileInputFiles", {"files": [str(path.resolve())], "backendNodeId": node})
            finally:
                try:
                    _checked(cdp, "Page.setInterceptFileChooserDialog", {"enabled": False})
                except Exception:
                    pass
            # Same bar as the DOM path: the SITE must show it took the file.
            end = time.monotonic() + UPLOAD_SETTLE_SEC + 3.0
            confirmed: list[str] = []
            while time.monotonic() < end:
                cdp.pump(0.25)
                confirmed, rejected = _app_evidence(cdp, before, path.name, req_mark)
                if rejected:
                    return UploadResult(False, f"the site rejected the file: {'; '.join(rejected[:3])}",
                                        candidates=labels)
                if any(c.startswith("page now shows") for c in confirmed) and \
                        not any(c.endswith("(in flight)") for c in confirmed):
                    break
            if not any(c.startswith("page now shows") for c in confirmed):
                return UploadResult(False, f"clicked {labels[0]!r} and answered its file chooser, but the "
                                    f"page never showed {path.name} ({'; '.join(confirmed[:2]) or 'no site response'})",
                                    candidates=labels)
            return UploadResult(True, f"{path.name} attached through the file chooser opened by "
                                f"{labels[0]!r} (site: {'; '.join(confirmed[:3])})",
                                labels[0], labels, int(node or 0), tab["webSocketDebuggerUrl"])
    return None


def upload_file(cdp_url: str, file_path: str, *, want: str = "resume", hint: str = "") -> UploadResult:
    """DOM file input first; if NO file input exists anywhere, the slot's own
    upload button with the file chooser intercepted (SuccessFactors shape)."""
    seen_token = _saw_pages.set(False)
    try:
        result = _upload_dom(cdp_url, file_path, want=want, hint=hint)
        reached = _saw_pages.get()
    finally:
        _saw_pages.reset(seen_token)
    if result.ok or not reached or not Path(file_path).is_file():
        return result
    w = (want or "resume").strip().lower()
    w = w if w in FIELD_HINTS else "resume"
    token = _deadline.set(time.monotonic() + FALLBACK_BUDGET_SEC)
    try:
        try:
            shown = _already_attached(cdp_url, Path(file_path), w)
        except Exception:
            shown = None
        if shown is not None:
            return shown
        if result.candidates:
            return result
        try:
            via = _upload_via_chooser(cdp_url, Path(file_path), w, hint)
        except Exception as exc:
            logger.warning("upload: file-chooser path raised: %s", exc)
            return result
    finally:
        _deadline.reset(token)
    if via is _CLICKED_NO_CHOOSER:
        # The click mounted an in-page upload dialog/menu instead: its input is
        # in the DOM now, so the ordinary path can take it.
        return _upload_dom(cdp_url, file_path, want=want, hint=hint)
    return via if via is not None else result


def _upload_dom(cdp_url: str, file_path: str, *, want: str = "resume", hint: str = "") -> UploadResult:
    """Attach with at most two writes and an overall bounded observation budget.

    Initial discovery scans every target to find popup forms. After selection,
    all reads/writes stay on that target and original document/frame/form.
    An immediate FileList is provisional: require continuous live-slot readback
    for UPLOAD_SETTLE_SEC, restarting the window on replacement or file loss.
    This is bounded evidence, not a promise against arbitrarily late page code.
    """
    path = Path(file_path)
    if not path.is_file():
        return UploadResult(False, f"file does not exist: {file_path}")
    want = (want or "resume").strip().lower()
    if want not in FIELD_HINTS:
        want = "resume"
    token = _deadline.set(time.monotonic() + UPLOAD_BUDGET_SEC)
    inflight_grace: list[bool] = []
    descriptors = []
    selected = None
    last = "no <input type=file> found in any open tab"
    consent = ""
    consent_at = None
    reloads = 0
    try:
        expected_size = path.stat().st_size
        with path.open('rb') as source:
            expected_sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
        # Readiness is polled, not a single fixed nap followed by a blind retry.
        while selected is None:
            _remaining()
            targets = find_file_inputs(cdp_url, hint)
            descriptors = [t.descriptor for t in targets]
            if targets:
                # Not "no <input type=file> found": that prefix told Myrtleby's
                # agent there was no input while two were listed, and counts
                # as `upload_no_file_input` in the driver.
                last = f"{len(targets)} file input(s) found"
                selected = _select(targets, path.name, want, hint)
                last = "attachment not yet durably verified in selected scope"
                break
            time.sleep(_remaining(UPLOAD_POLL_SEC))
        with _Cdp(selected.ws_url) as cdp:
            _checked(cdp, "DOM.enable")
            _checked(cdp, "Runtime.enable")
            # Page: answer the alert some ATSs raise after upload (it would
            # otherwise freeze the page). Network: see the app's own upload call.
            _checked(cdp, "Page.enable")
            _checked(cdp, "Network.enable")
            main_frame = _checked(cdp, "Page.getFrameTree")["frameTree"]["frame"]["id"]
            page_addr = _page_address(cdp)[0]
            before = _app_state(cdp, path.name)
            req_mark = set(cdp.requests)
            writes = 0
            resent = False
            stable_since = None
            previous_node = None
            empty_since = None
            consumed_since = None
            chip_since = None
            while True:
                _remaining()
                if writes and main_frame in cdp.navigating:
                    # The write sent the page away (Hirebridge posts the whole
                    # form back on change). The old page's input proves nothing
                    # now, and a re-sent change event would post it twice.
                    _grant_inflight_grace(["page reload"], inflight_grace)
                    stable_since = None
                    last = "the page started reloading after the write and the new page has not loaded"
                    cdp.pump(_remaining(UPLOAD_POLL_SEC))
                    continue
                if writes and not consent.startswith("accepted"):
                    consent = _answer_consent(cdp) or consent
                    if consent.startswith("accepted"):
                        consent_at = time.monotonic()
                try:
                    current = _live_slot(cdp, selected, path.name, want)
                except RuntimeError as exc:
                    if str(exc) != _SCOPE_DRIFT:
                        raise
                    rebound = _rebind_after_reload(cdp, selected, page_addr, path.name, want)
                    if rebound is None:
                        cdp.pump(_remaining(UPLOAD_POLL_SEC))
                        continue
                    selected = rebound
                    previous_node = stable_since = empty_since = consumed_since = chip_since = None
                    if writes:
                        reloads += 1
                        confirmed, rejected = _app_evidence(cdp, before, path.name, req_mark)
                        if rejected:
                            last = "site-side check after the page reloaded"
                            raise RuntimeError(f"the site rejected the file: {'; '.join(rejected[:3])}")
                        if _page_carries_file(cdp, path.name):
                            return UploadResult(True,
                                f"{path.name} sent to the site: the page posted the form back with the file "
                                f"and reloaded, and the reloaded page carries {path.name} ({writes} write(s)). "
                                "Its file input is empty again; that is expected, the site already has the file",
                                selected.descriptor, descriptors, 0, selected.ws_url)
                        last = ("the page reloaded itself after the write (the site posted the form back) "
                                "and the reloaded page does not show the file")
                    continue
                if current is None and writes:
                    # Greenhouse (Eastlake 1266, measured live): on change the app
                    # uploads the file to S3, then REMOVES <input id=resume> and
                    # renders its own "resume.pdf  [Remove file]" chip. The input
                    # is gone because the site took the file, not because it was
                    # lost. Accept only on the site's own evidence (file name now
                    # shown, upload call answered) with no rejection, held for
                    # the settle window; otherwise keep failing as before.
                    stable_since = None
                    confirmed, rejected = _app_evidence(cdp, before, path.name, req_mark)
                    if rejected:
                        last = "site-side check after attach"
                        raise RuntimeError(
                            f"the site rejected the file: {'; '.join(rejected[:3])}")
                    in_flight = [c for c in confirmed if c.endswith("(in flight)")]
                    _grant_inflight_grace(in_flight, inflight_grace)
                    shown = [c for c in confirmed if c.startswith("page now shows")]
                    # Oracle's "Import your profile from resume" (Trey 272, GHO-70)
                    # takes the file, stores it as the application's attachment
                    # and moves on to the next step: input gone, name never shown.
                    stored = _stored(confirmed)
                    now = time.monotonic()
                    if (shown or stored) and not in_flight:
                        if chip_since is None:
                            chip_since = now
                        if now - chip_since >= UPLOAD_SETTLE_SEC:
                            how = ("the site replaced the input with its own file display" if shown else
                                   "the site took the file and stored it as an attachment (the input was "
                                   "consumed, e.g. a profile import that moved on to the next step)")
                            return UploadResult(True,
                                f"{path.name} attached to {selected.descriptor}; {how} ({writes} write(s)"
                                + ("; after a re-sent change event" if resent else "")
                                + f"; site: {'; '.join(confirmed[:3])})" + (f"; {consent}" if consent else ""),
                                selected.descriptor, descriptors, 0, selected.ws_url)
                    else:
                        chip_since = None
                    # Say what the site did answer: "parsed but stored nothing"
                    # and "no call at all" need different fixes.
                    last = (f"site upload still in flight: {in_flight[0]}" if in_flight else
                            "upload input disappeared after the write and the page does not show the file"
                            + (f" (site: {'; '.join(confirmed[:3])}; no attachment stored)" if confirmed
                               else " (no upload call from the site)"))
                elif current is None:
                    stable_since = None
                    empty_since = None
                    last = "original logical upload slot missing; refusing another tab/slot"
                else:
                    chip_since = None
                    attached, seen = _verify_attached(cdp, path.name, current.backend_node_id,
                        expected_size=expected_size, expected_sha256=expected_sha256)
                    if seen.startswith("verification failed:"):
                        raise RuntimeError(seen)
                    if seen.startswith("DETACHED"):
                        # A stale read is neither file loss nor permission to write
                        # the old node. Rediscover without consuming the retry.
                        stable_since = None
                        empty_since = None
                        last = seen
                        cdp.pump(_remaining(UPLOAD_POLL_SEC))
                        continue
                    now = time.monotonic()
                    if current.backend_node_id != previous_node:
                        stable_since = None
                        empty_since = None
                    previous_node = current.backend_node_id
                    if attached and writes:
                        empty_since = None
                        if stable_since is None:
                            stable_since = now
                        # A FileList on the input is necessary, not sufficient:
                        # job 721's input held resume.pdf while Alpineer's page said
                        # "Unable to upload the resume". Ask the app too.
                        confirmed, rejected = _app_evidence(cdp, before, path.name, req_mark)
                        if rejected:
                            last = "site-side check after attach"
                            raise RuntimeError(
                                f"the file is on the input but the site rejected it: {'; '.join(rejected[:3])}")
                        in_flight = [c for c in confirmed if c.endswith("(in flight)")]
                        _grant_inflight_grace(in_flight, inflight_grace)
                        if in_flight:
                            stable_since = now  # wait for the app's own upload to answer
                            last = f"site upload still in flight: {in_flight[0]}"
                        elif not confirmed and not resent and now - stable_since >= UPLOAD_SETTLE_SEC:
                            # The input holds our file and the app has shown no
                            # sign of reacting. Greenhouse's form missed the
                            # change event DOM.setFileInputFiles fires (2022,
                            # 2119, 516 in the 2026-10-22 night run: no S3 POST,
                            # no file chip, "Resume/CV is required." on Submit);
                            # one page-dispatched change made 516's Submit go
                            # through. Re-send it once, here only: an app that
                            # reacted to the first event (an upload call or a
                            # chip) never gets a second one, so nothing that
                            # uploads on change uploads twice (see _attach).
                            _call_on_node(cdp, current.backend_node_id, _RESEND_CHANGE_JS)
                            resent = True
                            stable_since = now
                            last = "re-sent the change event; waiting for the site to react"
                        elif now - stable_since >= UPLOAD_SETTLE_SEC:
                            # The last operation before success is the exact live
                            # FileList read above, after a fresh scoped DOM walk,
                            # plus the app's own state read just now.
                            proof = "; ".join(confirmed[:3]) or (
                                "no site-side upload call or file-name display seen, not even after the change "
                                "event was re-sent (file held by the input until submit)")
                            if resent and confirmed:
                                proof = "reacted only to a re-sent change event: " + proof
                            revealed = _reveal_hidden_section(cdp, current.backend_node_id, path.name)
                            return UploadResult(True,
                                f"{path.name} attached to {current.descriptor} (verified after settle; "
                                f"{writes} write(s); site: {proof})" + (f". {revealed}" if revealed else "")
                                + (f"; {consent}" if consent else ""),
                                current.descriptor, descriptors, current.backend_node_id, current.ws_url)
                    else:
                        stable_since = None
                        last = f"live logical slot reports files=[{seen}]"
                        if empty_since is None:
                            empty_since = now
                        if writes and seen == "(none)":
                            # Some widgets take the File into their own state on
                            # `change` and EMPTY the input (SmartRecruiters
                            # spl-dropzone: input.files=0 within 15 ms, then a
                            # "resume.pdf" chip in its shadow root). An empty
                            # input is then success, not loss, but only on the
                            # app's own evidence; rewriting would upload twice.
                            confirmed, rejected = _app_evidence(cdp, before, path.name, req_mark)
                            if rejected:
                                last = "site-side check after attach"
                                raise RuntimeError(
                                    f"the site rejected the file: {'; '.join(rejected[:3])}")
                            in_flight = [c for c in confirmed if c.endswith("(in flight)")]
                            _grant_inflight_grace(in_flight, inflight_grace)
                            shown = any(c.startswith("page now shows") for c in confirmed)
                            if confirmed:
                                # The app acknowledged THIS write (upload call
                                # in flight or answered 2xx, or its chip shows
                                # the name). Never write again: a second write
                                # is a second upload (live: SR answered the
                                # duplicate with HTTP 403). Success needs the
                                # app's own display of the file, held for the
                                # settle window; otherwise wait out the budget.
                                empty_since = now
                                if shown and not in_flight:
                                    if consumed_since is None:
                                        consumed_since = now
                                    elif now - consumed_since >= UPLOAD_SETTLE_SEC:
                                        proof = "; ".join(confirmed[:3])
                                        return UploadResult(True,
                                            f"{path.name} taken by the upload widget at {current.descriptor} "
                                            f"(the widget cleared its input and shows the file; "
                                            f"{writes} write(s); site: {proof})"
                                            + (f"; {consent}" if consent else ""),
                                            current.descriptor, descriptors, current.backend_node_id,
                                            current.ws_url)
                                else:
                                    consumed_since = None
                                last = ("the upload widget cleared its input; waiting for the site to "
                                        f"show the file (site: {'; '.join(confirmed[:2])})")
                                cdp.pump(_remaining(UPLOAD_POLL_SEC))
                                continue
                            consumed_since = None
                            if consent_at is not None and now - consent_at < UPLOAD_SETTLE_SEC:
                                # The site's upload starts only once its consent
                                # dialog is answered, which just happened; a write
                                # now would reopen the dialog (Munson 2623).
                                cdp.pump(_remaining(UPLOAD_POLL_SEC))
                                continue
                        if writes >= 2:
                            raise RuntimeError(last + "; retry limit reached")
                        # A short readiness window avoids consuming the only retry
                        # on a transient empty replacement. All waiting is bounded.
                        if writes == 0 or now - empty_since >= NAV_SETTLE_SEC:
                            _attach(cdp, current, path)
                            writes += 1
                            empty_since = None
                cdp.pump(_remaining(UPLOAD_POLL_SEC))
    except Exception as exc:
        pending = f"; {consent}" if consent and not consent.startswith("accepted") else ""
        if reloads:
            pending += (f"; the page posted the form back and reloaded after {reloads} write(s), and the "
                        "reloaded page never showed the file")
        return UploadResult(False, f"{last}; {exc}{pending}", candidates=descriptors)
    finally:
        _deadline.reset(token)


# Eightfold (Tailspin 1745, measured on jobs.tailspin.com 2026-10-15) takes
# the file on `change`, then its beforeUpload awaits ensureConsent(): a
# role=dialog "Data Privacy Agreement" with Cancel / I Agree. Until it is
# answered nothing is POSTed; the widget remounts an EMPTY input, so the slot
# read "upload input disappeared after the write" every time. The agent's
# form-view snapshot never showed the dialog, so it re-requested the upload
# 4 times into the same wait. Clicking "I Agree" POSTs /resume_upload and the
# file chip appears. The agent is told to accept every consent (prompt rule
# 9), so the uploader answers a consent dialog the same way: only an enabled
# button whose whole label is an agree/accept, inside a visible modal whose
# text is about privacy/consent/terms and that holds no file input. Nothing
# else is clicked; a dialog
# without such a button is reported to the agent instead.
# Munson's Eightfold site (2623, GHO-85) names it "Data Privacy Notice" and
# answers with "I understand" (beside Close / Cancel); that is the same accept.
# Live (19:18 run, 1745/1757, GHO-26) the dialog was never found: the dialog
# fades in through CSS transitions on opacity/visibility, and the form-worker
# pages were hidden (Wayland windows GNOME suspends when covered, minimized or
# on another workspace), where animations never advance. The dialog sat open
# (class `..._visible__`) at visibility:hidden, opacity 0, with every
# transition at currentTime 0. Measured on a hidden tab: no POST until the
# transitions are finished; then "I Agree" POSTs /resume_upload and the chip
# shows the file, all without the page ever becoming visible.
_CONSENT_TEXT_RE = r"privacy|consent|data protection|terms|agree"
_CONSENT_BUTTON_RE = (r"^(?:yes,?\s*)?(?:i\s+)?(?:agree|accept|consent|understand)"
                      r"(?:\s+(?:and|&)\s+(?:continue|proceed|upload))?$")
_CONSENT_JS = r"""function(textRe, btnRe){
  const tRe = new RegExp(textRe, 'i'), bRe = new RegExp(btnRe, 'i');
  const vis = (e) => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (b) => (b.innerText || b.value || b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  for (const d of document.querySelectorAll('[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]')) {
    // A modal that holds a file input is the form itself (TurboHire renders
    // the whole application, privacy checkbox included, in a role=dialog).
    if (d.querySelector('input[type=file]')) continue;
    // A page that is not on screen (background tab, or a form-worker window
    // GNOME has suspended) never advances CSS animations, so a dialog opening
    // with a fade stays at visibility:hidden, opacity 0 (see the note above).
    // Run its finite entrance animations to their end, as a page on screen
    // would; endless ones (spinners) are left alone.
    if (!vis(d)) {
      for (const a of d.getAnimations({subtree: true})) {
        if (a.playState === 'running' && Number.isFinite(a.effect.getTiming().iterations)) a.finish();
      }
      if (!vis(d)) continue;
    }
    const text = (d.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text || !tRe.test(text)) continue;
    const btns = [...d.querySelectorAll('button,[role=button],input[type=button],input[type=submit]')].filter(vis);
    const head = d.querySelector('h1,h2,h3,h4,[role=heading],[class*=title i],[class*=header i]');
    const title = ((head && head.innerText) || text).replace(/\s+/g, ' ').trim().slice(0, 60);
    const agree = btns.find((b) => bRe.test(label(b)) && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
    if (agree) agree.click();
    return JSON.stringify({title, clicked: agree ? label(agree) : '', buttons: btns.map(label).filter(Boolean).slice(0, 6)});
  }
  return '';
}"""


def _answer_consent(cdp: _Cdp) -> str:
    """Accept a consent dialog the write opened; '' when there is none."""
    try:
        res = _checked(cdp, "Runtime.evaluate", {
            "expression": f"({_CONSENT_JS})({json.dumps(_CONSENT_TEXT_RE)}, {json.dumps(_CONSENT_BUTTON_RE)})",
            "returnByValue": True})
        raw = res.get("result", {}).get("value") or ""
    except Exception:
        return ""
    if not raw:
        return ""
    found = json.loads(raw)
    if found["clicked"]:
        logger.info("upload: accepted consent dialog %r (%s)", found["title"], found["clicked"])
        return f'accepted the site\'s "{found["title"]}" dialog ("{found["clicked"]}") that the upload opened'
    return (f'the site opened a "{found["title"]}" dialog after the write and the upload waits on it '
            f'(buttons: {" | ".join(found["buttons"]) or "none"}); answer it, then check the page shows '
            "the file before asking again")


# Avature's first application step ("First time here?  From Device / Copy &
# Paste") keeps the resume input AND that step's own Continue button inside
# <fieldset id="methodButton--fileFieldSetContainer" style="display:none">,
# shown only when the "From Device" toggle (#methodButton--file) is clicked.
# The uploader can attach to the hidden input, so the upload "succeeds" while
# the page still shows no Continue. Southgate job 1419 (2026-10-03) then spent
# 26 of its 80 steps (snapshots, vision, 11 console probes) finding that
# button. Clicking the toggle from a script, after the file is attached:
#   * shows the section and its Continue (measured on careers.wingtip.com and
#     southgatecareers.com),
#   * keeps the attached file (files.length stays 1),
#   * opens NO native file chooser (no user gesture; Page.fileChooserOpened
#     never fired), unlike a real click on the same toggle.
# Only a toggle the page itself ties to the hidden container is clicked (the
# Avature id pair `<id>` / `<id>FieldSetContainer`, or aria-controls); nothing
# else on the page is touched, so this cannot submit or navigate.
_REVEAL_JS = r"""
function(fileName) {
  const shown = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden';
  if (shown(this)) return '';
  let box = null;
  for (let p = this.parentElement; p && p !== document.body; p = p.parentElement) {
    if (getComputedStyle(p).display === 'none') box = p;
  }
  if (!box || !box.id) return '';
  const root = this.getRootNode();
  let toggle = null;
  if (box.id.endsWith('FieldSetContainer'))
    toggle = root.getElementById(box.id.slice(0, -'FieldSetContainer'.length));
  if (!toggle) toggle = root.querySelector('[aria-controls="' + CSS.escape(box.id) + '"]');
  if (!toggle || toggle.contains(box) || box.contains(toggle) || !shown(toggle)) return '';
  if (toggle.matches('[type=submit], a[href]:not([href="#"]):not([href^="javascript"])')) return '';
  toggle.click();
  if (!shown(box)) return '';
  const files = [...(this.files || [])].map(f => f.name);
  if (!files.includes(fileName)) return 'LOST';
  const btns = [...box.querySelectorAll('button, input[type=submit], [role=button]')]
    .filter(shown).map(b => (b.innerText || b.value || '').trim()).filter(Boolean);
  const label = (toggle.innerText || toggle.getAttribute('aria-label') || '').trim();
  return 'The upload section was hidden until "' + label + '" was opened; the pipeline opened it' +
    (btns.length ? ', so its "' + btns.join('" / "') + '" button is now visible. Click that button to continue' : '');
}
"""


def _reveal_hidden_section(cdp: _Cdp, backend_node_id: int, file_name: str) -> str:
    """Show the collapsed section that holds the just-filled input (see _REVEAL_JS)."""
    try:
        obj = _checked(cdp, "DOM.resolveNode", {"backendNodeId": backend_node_id})["object"]["objectId"]
        res = _checked(cdp, "Runtime.callFunctionOn", {
            "objectId": obj,
            "functionDeclaration": _REVEAL_JS,
            "arguments": [{"value": file_name}],
            "returnByValue": True,
        })
        note = (res.get("result") or {}).get("value") or ""
    except Exception as e:  # never turn a verified upload into a failure
        logger.debug("reveal hidden upload section failed: %s", e)
        return ""
    if note == "LOST":
        logger.warning("opening the hidden upload section dropped the file")
        return ""
    if note:
        logger.info("upload: %s", note)
    return note


def _upload_once(cdp_url: str, file_path: str, *, want: str = "resume", hint: str = "") -> UploadResult:
    """Compatibility entrypoint; no provisional success escapes this module."""
    return upload_file(cdp_url, file_path, want=want, hint=hint)


def parse_request(line: str) -> Optional[tuple[str, str]]:
    """Parse a `NEED_UPLOAD: <resume|cover> | <hint>` directive."""
    m = re.match(
        r"^NEED_UPLOAD:\s*(?P<want>resume|cover|cover_letter|cover-letter|cv)?"
        r"\s*(?:\|\s*(?P<hint>.+?))?\s*$",
        line.strip(),
        re.I,
    )
    if not m:
        return None
    want = (m.group("want") or "resume").lower()
    if want in ("cover_letter", "cover-letter"):
        want = "cover"
    if want == "cv":
        want = "resume"
    return want, (m.group("hint") or "").strip()
