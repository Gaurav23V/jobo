"""LinkedIn Easy Apply: upload THIS job's resume, select its card, gate Submit.

TODO item 18 (2026-10-11 run): 13 of 27 applications went out through Easy
Apply with LinkedIn's saved, pre-selected resume (the user's old one). See
hermes_plugin/jobo-browser/resume_guard.py for the real modal's structure and
the decision rule; this module loads that file by path so the driver and the
Hermes plugin share one probe and one rule.

Driver-side pieces:
  * upload_and_select(): the NEED_UPLOAD path while an Easy Apply modal is
    open. Clicks "Upload resume" with the file chooser intercepted (the real
    modal has no <input type=file> until then), answers the chooser with this
    job's uniquely named copy, waits for LinkedIn to list that card, makes sure
    it is the SELECTED card (one trusted click on it if not) and reads the
    selection back. Never touches Next/Review/Submit.
  * submit_gate(): called before every NEED_CLICK that could submit. Refuses
    while the modal's selected resume is not this job's file.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from module4 import uploader

logger = logging.getLogger(__name__)

_GUARD_PATH = Path(__file__).resolve().parents[1] / "hermes_plugin" / "jobo-browser" / "resume_guard.py"
_spec = importlib.util.spec_from_file_location("jobo_resume_guard_shared", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(guard)  # type: ignore[union-attr]

CHOOSER_WAIT_SEC = 4.0
CARD_WAIT_SEC = 25.0
SELECT_SETTLE_SEC = 1.0


@dataclass
class EasyApplyUpload:
    ok: bool
    detail: str
    selected: list[str] = field(default_factory=list)
    already: bool = False

    def as_agent_message(self) -> str:
        if self.ok:
            head = "UPLOAD_DONE: ALREADY_ATTACHED " if self.already else "UPLOAD_DONE: "
            return (
                f"{head}{self.detail}\n\n"
                "LinkedIn Easy Apply now has THIS job's resume uploaded and selected. Do NOT "
                "click any other resume card (the saved ones are old resumes), do not "
                "deselect this one and do not ask for another upload. Continue with the "
                "remaining steps and submit with NEED_CLICK."
            )
        return (
            f"UPLOAD_FAILED: {self.detail}\n"
            "Make sure the Easy Apply modal is on its Resume step (the step listing resume "
            "cards with an \"Upload resume\" button; use Back if you passed it), then ask "
            "NEED_UPLOAD: resume again. Never submit with a saved resume instead."
        )


def _linkedin_page_targets(cdp_url: str) -> list[dict]:
    return [t for t in uploader.list_page_targets(cdp_url) if guard.is_linkedin(t.get("url", ""))]


def _eval(cdp: uploader._Cdp, expression: str):
    msg = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                        "awaitPromise": True})
    if msg.get("error"):
        raise RuntimeError(f"Runtime.evaluate: {msg['error']}")
    res = msg.get("result") or {}
    if res.get("exceptionDetails"):
        raise RuntimeError("page script failed: " + json.dumps(res["exceptionDetails"])[:200])
    return (res.get("result") or {}).get("value")


def _state(cdp: uploader._Cdp, point: list | None = None) -> dict:
    raw = _eval(cdp, f"({guard.INSPECT_JS})({json.dumps(point)})")
    return json.loads(raw) if isinstance(raw, str) else {"modal": False}


def open_modal(cdp_url: str) -> tuple[dict, dict] | None:
    """(page target, state) of the first LinkedIn tab with an Easy Apply modal."""
    for tab in _linkedin_page_targets(cdp_url):
        try:
            with uploader._Cdp(tab["webSocketDebuggerUrl"]) as cdp:
                st = _state(cdp)
        except Exception as e:
            logger.debug("easy_apply: could not read %s: %s", tab.get("url", "")[:80], e)
            continue
        if st.get("modal"):
            return tab, st
    return None


# Points to click inside the open modal. Reads only; the click is a trusted
# CDP mouse event sent by Python, never a synthetic DOM event.
_POINT_JS = r"""(what, name) => {
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const d = [...document.querySelectorAll('dialog[open], [role=dialog], .jobs-easy-apply-modal, [data-test-modal]')].find(vis);
  if (!d) return JSON.stringify({err: 'no Easy Apply modal is open'});
  let cands = [];
  if (what === 'upload') {
    // Innermost control whose own text says "Upload resume".
    cands = [...d.querySelectorAll('button, [role=button], label')].filter(vis)
      .filter(b => /\bupload resume\b/i.test(norm(b.innerText || b.getAttribute('aria-label'))))
      .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length).slice(0, 1);
    if (!cands.length) return JSON.stringify({err: 'no "Upload resume" button in the modal'});
  } else {
    const radios = [...d.querySelectorAll('input[type=radio]')]
      .filter(r => norm(r.getAttribute('aria-label') || (r.labels && r.labels[0] ? r.labels[0].innerText : '')) === name);
    if (!radios.length) return JSON.stringify({err: 'no resume card named ' + name});
    const r = radios[0];
    // The visible thing to press: the radio, its label, or the card button.
    cands = [r, ...(r.labels || [])];
    let n = r.parentElement;
    for (let k = 0; n && n !== d && k < 6; n = n.parentElement, k++) {
      const card = [...n.querySelectorAll('[role=button]')].find(c => norm(c.innerText).includes(name));
      if (card) { cands.push(card); break; }
    }
  }
  const tried = [];
  for (const el of cands) {
    if (!vis(el)) continue;
    el.scrollIntoView({block: 'center', inline: 'center'});
    const b = el.getBoundingClientRect();
    if (b.width < 3 || b.height < 3) continue;
    const x = b.left + b.width / 2, y = b.top + b.height / 2;
    const hit = document.elementFromPoint(x, y);
    const ok = !!hit && (hit === el || el.contains(hit) || hit.contains(el) ||
      (hit.tagName === 'LABEL' && !!hit.htmlFor && hit.htmlFor === el.id));
    if (ok) return JSON.stringify({x, y, ok, hit: hit.tagName});
    tried.push(hit ? hit.tagName + '.' + String(hit.className).slice(0, 30) : 'nothing');
  }
  return JSON.stringify({err: (what === 'upload' ? '"Upload resume"' : 'the ' + name + ' card') +
    ' is covered or not visible (hit: ' + tried.join(', ') + ')'});
}"""

_DIALOG_FILE_INPUTS_JS = r"""(() => {
  const d = [...document.querySelectorAll('dialog[open], [role=dialog], .jobs-easy-apply-modal')][0];
  return d ? d.querySelectorAll('input[type=file]').length : 0;
})()"""

# Real LinkedIn answers a good upload with role=alert "Resume uploaded
# successfully" (seen 2026-10-12), so an alert is only a rejection when its
# text reads like one.
_ERRORS_JS = r"""(() => {
  const d = [...document.querySelectorAll('dialog[open], [role=dialog], .jobs-easy-apply-modal')][0];
  if (!d) return [];
  const bad = /fail|error|could ?n['’o]?t|unable|too large|exceeds|not supported|invalid|try again/i;
  return [...d.querySelectorAll('[role=alert], .artdeco-inline-feedback--error, [data-test-form-element-error-messages]')]
    .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
    .filter(t => t && bad.test(t) && !/success/i.test(t)).slice(0, 3);
})()"""


def _press(cdp: uploader._Cdp, x: float, y: float) -> None:
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                              "button": "left", "clickCount": 1})


def _point(cdp: uploader._Cdp, what: str, name: str = "") -> dict:
    raw = _eval(cdp, f"({_POINT_JS})({json.dumps(what)}, {json.dumps(name)})")
    return json.loads(raw) if isinstance(raw, str) else {"err": "unreadable"}


def _select(cdp: uploader._Cdp, name: str) -> tuple[bool, dict, str]:
    """Make `name` the selected card. At most one trusted click."""
    st = _state(cdp)
    if st.get("selected") == [name]:
        return True, st, "already selected"
    pt = _point(cdp, "card", name)
    if pt.get("err") or not pt.get("ok"):
        return False, st, f"could not click the {name} card ({pt.get('err') or 'covered by ' + pt.get('hit', '?')})"
    _press(cdp, pt["x"], pt["y"])
    end = time.monotonic() + SELECT_SETTLE_SEC + 2.0
    while time.monotonic() < end:
        cdp.pump(0.25)
        st = _state(cdp)
        if st.get("selected") == [name]:
            # Hold it: a re-render can revert a selection a moment later.
            cdp.pump(SELECT_SETTLE_SEC)
            st = _state(cdp)
            if st.get("selected") == [name]:
                return True, st, "selected with one click"
    return False, st, f"clicked the {name} card but the selection is {st.get('selected')}"


def upload_and_select(cdp_url: str, file_path: str) -> EasyApplyUpload | None:
    """None when no Easy Apply modal with a resume step is open (caller uses
    the generic uploader). Otherwise a verified result for the agent."""
    path = Path(file_path)
    if not path.is_file():
        return EasyApplyUpload(False, f"file does not exist: {file_path}")
    name = path.name
    found = open_modal(cdp_url)
    if not found:
        return None
    tab, st = found
    if not st.get("cards") and not st.get("upload_button"):
        # Not on the resume step (contact or questions page). Let the generic
        # uploader answer; its "no file input yet" reply tells the agent to
        # click through to the step first.
        return None
    with uploader._Cdp(tab["webSocketDebuggerUrl"]) as cdp:
        for m in ("DOM.enable", "Runtime.enable", "Page.enable"):
            cdp.call(m)
        if any(c.get("name") == name for c in st.get("cards") or []):
            ok, st, how = _select(cdp, name)
            if ok:
                return EasyApplyUpload(True, f"{name} was already uploaded in this modal; its card "
                                             f"is the selected resume ({how})", st.get("selected") or [], True)
            return EasyApplyUpload(False, how, st.get("selected") or [])
        before_cards = len(st.get("cards") or [])
        attached = ""
        if int(_eval(cdp, _DIALOG_FILE_INPUTS_JS) or 0) > 0:
            # Older Easy Apply markup keeps a hidden input in the modal.
            res = uploader.upload_file(cdp_url, str(path), want="resume", hint="")
            if not res.ok:
                return EasyApplyUpload(False, f"the modal's file input did not take the file: {res.detail}")
            attached = "through the modal's file input"
        else:
            pt = _point(cdp, "upload")
            if pt.get("err") or not pt.get("ok"):
                return EasyApplyUpload(False, f"cannot press \"Upload resume\": {pt.get('err') or 'covered by ' + pt.get('hit', '?')}")
            cdp.call("Page.setInterceptFileChooserDialog", {"enabled": True})
            try:
                _press(cdp, pt["x"], pt["y"])
                end = time.monotonic() + CHOOSER_WAIT_SEC
                while not cdp.choosers and time.monotonic() < end:
                    cdp.pump(0.2)
                if not cdp.choosers:
                    return EasyApplyUpload(False, "\"Upload resume\" was pressed but no file chooser opened")
                node = cdp.choosers[0].get("backendNodeId")
                msg = cdp.call("DOM.setFileInputFiles", {"files": [str(path.resolve())], "backendNodeId": node})
                if msg.get("error"):
                    return EasyApplyUpload(False, f"the file chooser refused the file: {msg['error']}")
            finally:
                try:
                    cdp.call("Page.setInterceptFileChooserDialog", {"enabled": False})
                except Exception:
                    pass
            attached = "through \"Upload resume\" (file chooser answered)"
        # LinkedIn stores the file, then lists it as a new card.
        end = time.monotonic() + CARD_WAIT_SEC
        while time.monotonic() < end:
            cdp.pump(0.4)
            st = _state(cdp)
            if any(c.get("name") == name for c in st.get("cards") or []):
                break
            errs = _eval(cdp, _ERRORS_JS) or []
            if errs:
                return EasyApplyUpload(False, f"LinkedIn rejected the upload: {'; '.join(errs)}")
        else:
            return EasyApplyUpload(False, f"uploaded {name} {attached}, but no resume card with that "
                                          f"name appeared within {int(CARD_WAIT_SEC)}s "
                                          f"(cards before {before_cards}, now {len(st.get('cards') or [])})")
        ok, st, how = _select(cdp, name)
        if not ok:
            return EasyApplyUpload(False, f"uploaded {name}, but {how}", st.get("selected") or [])
        return EasyApplyUpload(True, f"{name} uploaded {attached}; LinkedIn lists it and its card is "
                                     f"the selected resume ({how})", st.get("selected") or [])


def submit_gate(cdp_url: str, expected: str, verified: bool) -> tuple[bool, str, bool]:
    """(allowed, reason, easy_apply_open) before a click that may submit.

    Fails CLOSED when a LinkedIn tab exists but cannot be read: an Easy Apply
    modal may be open on it, and a submission cannot be undone.
    """
    tabs = _linkedin_page_targets(cdp_url)
    if not tabs:
        return True, "no LinkedIn tab", False
    try:
        states = guard.inspect_pages(cdp_url.rstrip("/"))
    except Exception as e:
        return False, f"could not read the LinkedIn tab to verify the selected resume ({e})", True
    for st in states:
        if st.get("modal"):
            ok, reason = guard.judge(st, expected, verified)
            return ok, reason, True
    return True, "no LinkedIn Easy Apply modal is open", False
