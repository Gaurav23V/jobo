"""No LinkedIn Easy Apply submission without THIS job's own resume selected.

What happened (2026-10-11, run_20261011_1555): 13 of 27 applications were sent
through LinkedIn Easy Apply with zero uploads. Easy Apply's resume step lists
every resume LinkedIn has stored as radio "cards" and pre-selects one; the
file input only exists behind an "Upload resume" button. The agent saw no
upload field, accepted the pre-selected card (the user's OLD resume, named
resume.pdf) and asked NEED_CLICK: Submit application. Nothing checked.

Real modal, captured read-only from the user's signed-in browser on 2026-10-12:

    <dialog data-testid="dialog" aria-labelledby="dialog-header" open>
      <h2 id="dialog-header">Apply to <Company></h2>
      ... contact fields ...
      <fieldset role="radiogroup">            <- 90+ saved resume cards
        <div><div role="button">PDF resume.pdf 10/11/2026</div>
             <a href="https://www.linkedin.com/ambry/...">download</a></div>
        <div aria-label="resume.pdf">
          <input type="radio" aria-label="resume.pdf" checked
                 name="radio-group-..."><label for=...></label></div>
        ...
      </fieldset>
      <button type="button">Upload resume</button>   <- no <input type=file> in the DOM
      <button type="button">Submit application</button>
    </dialog>

This file is the ONE place that reads that modal and decides whether Submit
may happen:
  * INSPECT_JS reads the open Easy Apply modal: card names and which one is
    selected, the Upload/Next/Review/Submit buttons, document names shown in
    the text (the Review step shows the chosen resume as text, no radios).
  * judge() is the rule: Submit only when the selected resume is exactly this
    job's upload name (<First>_<Last>_Resume_<job>_<hash>.pdf, module4/
    upload_names.py), or, on a step that shows no resume at all, when the
    driver verified that card earlier in this job (state file).
  * check_tool_call() is the Hermes pre_tool_call veto: a browser_click on a
    "Submit ..." ref, Enter/Space on a focused Submit, a console/CDP script
    that clicks or submits, or a CDP mouse press that lands on Submit, while
    the modal shows the wrong resume, is BLOCKED before it reaches the page.
module4/easy_apply.py loads this file by path for the driver-side checks
(NEED_UPLOAD routing and the NEED_CLICK gate), so plugin and driver can never
disagree about what the modal shows.

The driver passes, through the agent's environment:
  JOBO_UPLOAD_RESUME_NAME   the file name our upload carries for this job
  JOBO_RESUME_GUARD_FILE    JSON {"verified": bool, ...} it rewrites after
                            every verified Easy Apply upload/selection
Without JOBO_UPLOAD_RESUME_NAME (not a jobo apply run) this guard does nothing.
Reading the page fails OPEN (an unreadable browser must not break every tool);
a rule that matches fails CLOSED.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ENV_NAME = "JOBO_UPLOAD_RESUME_NAME"
ENV_STATE = "JOBO_RESUME_GUARD_FILE"

# Runs inside a LinkedIn page. Returns a JSON string (see judge()).
# `pt` is an optional [x, y] viewport point: reports whether it hits Submit.
INSPECT_JS = r"""(pt) => {
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  const vis = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden';
  const DOC = /[^\s\/\\]+\.(?:pdf|docx?|rtf|odt|txt)\b/ig;
  const isDoc = s => /^[^\/\\]+\.(?:pdf|docx?|rtf|odt|txt)$/i.test(norm(s));
  const label = b => norm(b.innerText || b.value || b.getAttribute('aria-label') || '');
  const SUBMIT = /^submit(?: application)?$/i;
  const cands = [...document.querySelectorAll(
    'dialog[open], [role=dialog], [role=alertdialog], .jobs-easy-apply-modal, [data-test-modal]')].filter(vis);
  for (const d of cands) {
    const heading = norm((d.querySelector('#dialog-header, h1, h2, h3') || {}).innerText);
    const btns = [...d.querySelectorAll('button, [role=button], input[type=submit]')].filter(vis);
    const names = btns.map(label);
    const submit = btns.find(b => SUBMIT.test(label(b)));
    const upload = btns.find(b => /\bupload resume\b/i.test(label(b)));
    const docName = r => norm(r.getAttribute('aria-label') ||
      (r.labels && r.labels[0] ? r.labels[0].innerText : ''));
    // Document radios grouped by name. A modal can hold a resume group AND a
    // cover-letter group; only the resume group decides Submit.
    const groups = {};
    for (const r of d.querySelectorAll('input[type=radio]')) {
      if (!isDoc(docName(r))) continue;
      (groups[r.name || '_'] = groups[r.name || '_'] || []).push(r);
    }
    const kindOf = r => {
      for (let n = r.parentElement, k = 0; n && n !== d && k < 8; n = n.parentElement, k++) {
        const head = norm(n.innerText).slice(0, 40).toLowerCase();
        if (/^(resume|cv)\b/.test(head)) return 'resume';
        if (/^cover letter\b/.test(head)) return 'cover';
      }
      return '';
    };
    const named = Object.values(groups).map(rs => ({rs, kind: kindOf(rs[0])}));
    const pick = named.find(g => g.kind === 'resume') || named.find(g => g.kind !== 'cover');
    const radios = pick ? pick.rs : [];
    const cards = radios.map(r => ({name: docName(r), checked: !!r.checked}));
    // Only a real application modal: resume cards, the upload button, or an
    // "Apply to ..." heading with the modal's own Next/Review/Submit buttons.
    // Other LinkedIn dialogs (save prompt, share, messaging) never gate.
    const flowBtn = names.some(n => /^(next|review|submit application|continue to next step)$/i.test(n));
    const easy = cards.length > 0 || !!upload || d.matches('.jobs-easy-apply-modal') ||
      ((/^apply to\b/i.test(heading) || /easy apply/i.test(heading)) && flowBtn);
    if (!easy) continue;
    const text = d.innerText || '';
    const shown = [...new Set((text.match(DOC) || []).map(norm))];
    let hitSubmit = false;
    if (pt && submit) {
      // Inside Submit's box counts even when something covers it or it is
      // outside the viewport (elementFromPoint returns null there).
      const r = submit.getBoundingClientRect();
      const h = document.elementFromPoint(pt[0], pt[1]);
      hitSubmit = (pt[0] >= r.left && pt[0] <= r.right && pt[1] >= r.top && pt[1] <= r.bottom) ||
        (!!h && (h === submit || submit.contains(h)));
    }
    const act = document.activeElement;
    return JSON.stringify({
      modal: true, url: location.href, heading,
      cards, selected: cards.filter(c => c.checked).map(c => c.name),
      shown_docs: shown,
      upload_button: !!upload, submit_button: !!submit,
      buttons: names.filter(Boolean).slice(0, 20),
      active_is_submit: !!submit && !!act && (act === submit || submit.contains(act)),
      point_hits_submit: hitSubmit,
    });
  }
  return JSON.stringify({modal: false, url: location.href});
}"""

_SUBMIT_NAME = re.compile(r"\bsubmit\b", re.I)
_SCRIPT_SUBMIT = re.compile(r"submit", re.I)
_SCRIPT_ACTS = re.compile(r"\.click\s*\(|dispatchEvent|requestSubmit|\.submit\s*\(|MouseEvent|PointerEvent", re.I)


def is_linkedin(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def judge(state: dict | None, expected: str, verified: bool) -> tuple[bool, str]:
    """(may_submit, reason) for one inspected Easy Apply modal state."""
    if not state or not state.get("modal"):
        return True, "no LinkedIn Easy Apply modal is open"
    if not expected:
        return False, "this job has no resume file of its own to send"
    cards = state.get("cards") or []
    selected = state.get("selected") or []
    if cards:
        if selected == [expected]:
            return True, f"the selected resume card is this job's own file {expected}"
        if not selected:
            return False, "no resume card is selected"
        return False, (f"the selected resume card is {selected[0]!r}, a resume saved in "
                       f"LinkedIn, not this job's file {expected!r}")
    shown = state.get("shown_docs") or []
    if expected in shown:
        return True, f"this step shows this job's own file {expected}"
    # Our own cover letter for this job is not a competing resume.
    job = re.search(r"_Resume_(\d+)_[0-9a-f]{6}\.\w+$", expected)
    own_cover = re.compile(rf"\w+_Cover_Letter_{job.group(1)}_[0-9a-f]{{6}}\.\w+") if job else None
    others = [s for s in shown if not (own_cover and own_cover.fullmatch(s))]
    if others:
        return False, (f"this step shows {', '.join(repr(s) for s in others[:3])}, not this job's "
                       f"file {expected!r}")
    if verified:
        return True, "no resume shown on this step; this job's file was uploaded and selected earlier"
    return False, ("no resume of this job was uploaded and selected in this application "
                   "(the resume step was skipped or a saved resume was accepted)")


def refusal_message(reason: str, expected: str) -> str:
    return (
        f"BLOCKED by jobo: LinkedIn Easy Apply Submit refused: {reason}. The user's rule: every "
        "application carries THIS job's own resume, never a resume LinkedIn has saved "
        "(the pre-selected one is an old resume). Do this instead: go to the Resume step "
        "(use Back if you are past it) and end your turn with exactly\n"
        "NEED_UPLOAD: resume\n"
        f"The pipeline clicks \"Upload resume\", uploads {expected or 'this job’s resume'} and "
        "selects that card. Do not click another resume card after that. Then continue and "
        "submit again."
    )


# --- live page reading ----------------------------------------------------------


def _cdp_base() -> str | None:
    raw = os.environ.get("BROWSER_CDP_URL", "").strip()
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def _http_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ws_eval(ws_url: str, expression: str, timeout: float = 4.0):
    """Runtime.evaluate over one short-lived CDP websocket; returns the value."""
    msg = json.dumps({"id": 1, "method": "Runtime.evaluate",
                      "params": {"expression": expression, "returnByValue": True}})
    try:
        from websockets.sync.client import connect  # Hermes venv has websockets
    except ImportError:  # pragma: no cover - repo venv path
        connect = None
    if connect is not None:
        with connect(ws_url, open_timeout=timeout, close_timeout=1, max_size=None) as ws:
            ws.send(msg)
            while True:
                reply = json.loads(ws.recv(timeout=timeout))
                if reply.get("id") == 1:
                    break
    else:  # pragma: no cover
        import websocket  # websocket-client
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        try:
            ws.send(msg)
            while True:
                reply = json.loads(ws.recv())
                if reply.get("id") == 1:
                    break
        finally:
            ws.close()
    if reply.get("error"):
        raise RuntimeError(str(reply["error"])[:200])
    result = reply.get("result") or {}
    if result.get("exceptionDetails"):
        raise RuntimeError("inspect script failed: " + json.dumps(result["exceptionDetails"])[:200])
    return (result.get("result") or {}).get("value")


def inspect_pages(base: str, point: list | None = None) -> list[dict]:
    """Easy Apply state of every LinkedIn page target (modal or not)."""
    out = []
    for t in _http_json(f"{base}/json/list"):
        if t.get("type") != "page" or not is_linkedin(t.get("url", "")) or not t.get("webSocketDebuggerUrl"):
            continue
        expr = f"({INSPECT_JS})({json.dumps(point)})"
        raw = _ws_eval(t["webSocketDebuggerUrl"], expr)
        state = json.loads(raw) if isinstance(raw, str) else {"modal": False}
        state["target_id"] = t.get("id", "")
        out.append(state)
    return out


def _verified() -> bool:
    path = os.environ.get(ENV_STATE, "").strip()
    if not path:
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(json.load(fh).get("verified"))
    except (OSError, ValueError):
        return False


# --- the pre_tool_call veto ---------------------------------------------------------


def _submit_attempt(tool_name: str, args: dict, ref_name) -> tuple[bool, list | None, str]:
    """(gated, point, what): does this call try to press a Submit control?"""
    if tool_name == "browser_click":
        name = (ref_name(str(args.get("ref") or "").lstrip("@")) or "") if ref_name else ""
        return bool(_SUBMIT_NAME.search(name)), None, f"click on {name!r}"
    if tool_name == "browser_press":
        key = str(args.get("key") or "")
        return key in ("Enter", "NumpadEnter", " ", "Space"), None, f"key {key}"
    if tool_name == "browser_console":
        expr = str(args.get("expression") or "")
        return bool(_SCRIPT_SUBMIT.search(expr) and _SCRIPT_ACTS.search(expr)), None, "script"
    if tool_name == "browser_cdp":
        method = str(args.get("method") or "").lower()
        raw = args.get("params")
        params: dict = raw if isinstance(raw, dict) else {}
        if method in ("runtime.evaluate", "runtime.callfunctionon"):
            src = str(params.get("expression") or params.get("functionDeclaration") or "")
            return bool(_SCRIPT_SUBMIT.search(src) and _SCRIPT_ACTS.search(src)), None, "script"
        if method == "input.dispatchmouseevent" and params.get("type") == "mousePressed":
            try:
                return True, [float(params["x"]), float(params["y"])], "mouse press"
            except (KeyError, TypeError, ValueError):
                return False, None, ""
        if method == "input.dispatchkeyevent" and params.get("key") in ("Enter", " "):
            return True, None, "key event"
    return False, None, ""


def check_tool_call(tool_name: str, args: dict | None, ref_name=None) -> str | None:
    """Block message, or None to let the call through."""
    expected = os.environ.get(ENV_NAME, "").strip()
    if not expected:
        return None
    a = args if isinstance(args, dict) else {}
    gated, point, what = _submit_attempt(tool_name or "", a, ref_name)
    if not gated:
        return None
    base = _cdp_base()
    if not base:
        return None
    try:
        states = inspect_pages(base, point)
    except Exception as e:  # fail open: the driver's NEED_CLICK gate still applies
        logger.warning("jobo resume guard could not read the page: %s", e)
        return None
    verified = _verified()
    for state in states:
        if not state.get("modal"):
            continue
        if tool_name == "browser_press" and not state.get("active_is_submit"):
            continue
        if point is not None and not state.get("point_hits_submit"):
            continue
        if tool_name == "browser_cdp" and what == "key event" and not state.get("active_is_submit"):
            continue
        ok, reason = judge(state, expected, verified)
        if not ok:
            logger.warning("jobo resume guard blocked %s (%s): %s", tool_name, what, reason)
            return refusal_message(reason, expected)
    return None
