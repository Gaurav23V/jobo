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

Every other site (GHO-70, run_20261021_2350): Trey 272 (Oracle) and Margie
1635 (TurboHire) were submitted after UPLOAD_FAILED with no verified upload of
the job's resume, 1635 through a direct browser_click on "Apply For This Job".
Nothing checked outside Easy Apply. EXTERNAL_INSPECT_JS reads a non-LinkedIn
form: its resume upload slots and whether the page shows this job's file.
judge_external() refuses a submit while this job's resume is not verified and
either an upload of it failed or the form has a resume field. A form without
any resume field still submits.

A resumed draft (GHO-78, Verizon 2219 on 2026-10-22): Workday kept attempt 1's
upload of this job's file, the agent's attempt 2 never uploaded again and sent
the Review page with a direct browser_click on "Submit". The veto read the
page, found this job's own file name and let it through, but only the
driver's NEED_CLICK gate recorded that proof, so the audit called it a
violation. The veto now records it in a file next to the state file
(seen_file()), which the driver reads when it checks the resume rule. A page
showing another job's file (or an older copy of this job's) and not ours is
refused.

The driver passes, through the agent's environment:
  JOBO_UPLOAD_RESUME_NAME   the file name our upload carries for this job
  JOBO_RESUME_GUARD_FILE    JSON {"verified": bool, "resume_uploads_ok": int,
                            "resume_upload_failed": str} it rewrites after
                            every Easy Apply selection and every resume upload
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

# Runs inside any non-LinkedIn page or frame: args [pt, expected]. Returns a
# JSON string (see judge_external()).
EXTERNAL_INSPECT_JS = r"""(args) => {
  const pt = args[0], low = String(args[1] || '').toLowerCase();
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const RES = /\b(resume|résumé|cv|curriculum vitae)\b/i, COVER = /\bcover\b/i;
  const UPLOADISH = /upload|attach|browse|choose (a )?file|select (a )?file|drop (a |your )?file/i;
  const NEVER = /dropbox|google ?drive|onedrive|linkedin|indeed|remove|delete/i;
  const SUBMITISH = /\b(submit|apply|send|finish)\b/i;
  const roots = [document];
  const walk = r => { for (const el of r.querySelectorAll('*')) if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); } };
  try { walk(document); } catch (e) {}
  const own = el => norm([el.getAttribute('aria-label'), el.getAttribute('title'), el.id, el.getAttribute('name'),
    typeof el.className === 'string' ? el.className : '', el.innerText || el.value || '',
    ...(el.labels ? [...el.labels].map(l => l.innerText) : [])].join(' '));
  // Smallest ancestor that names a document kind: the slot this control belongs to.
  const near = el => {
    for (let n = el.parentElement || (el.getRootNode() || {}).host, k = 0; n && k < 5; n = n.parentElement || (n.getRootNode() || {}).host, k++) {
      const t = norm(n.innerText);
      if (t && t.length < 400 && (RES.test(t) || COVER.test(t))) return t;
    }
    return '';
  };
  const kind = el => {
    const o = own(el);
    if (COVER.test(o) && !RES.test(o)) return 'cover';
    if (RES.test(o) || RES.test(near(el))) return 'resume';
    return '';
  };
  const slots = [];
  let oursInInput = false;
  for (const r of roots) {
    for (const i of r.querySelectorAll('input[type=file]')) {
      if (i.disabled) continue;
      const names = [...(i.files || [])].map(f => f.name);
      if (names.some(n => n.toLowerCase() === low)) oursInInput = true;
      if (kind(i) === 'resume') slots.push({how: 'file input', desc: own(i).slice(0, 80), files: names});
    }
    for (const b of r.querySelectorAll('button, [role=button], input[type=button], label')) {
      if (!vis(b) || b.disabled) continue;
      const o = own(b);
      if (UPLOADISH.test(o) && !NEVER.test(o) && kind(b) === 'resume')
        slots.push({how: 'upload button', desc: o.slice(0, 80), files: []});
    }
  }
  let text = (document.body && document.body.innerText) || '';
  for (const r of roots.slice(1)) text += '\n' + (r.textContent || '');
  const shown = !!low && text.toLowerCase().includes(low);
  // Another of our per-job files (<First>_<Last>_Resume_<job>_<hash>.pdf): a
  // draft that kept some other job's upload, or an older copy of this job's.
  const stem = low.match(/^(.+_resume_)\d+_[0-9a-f]{6}\.\w+$/);
  const otherRe = stem ? new RegExp(stem[1].replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\d+_[0-9a-f]{6}\\.(?:pdf|docx?|rtf|odt|txt)', 'g') : null;
  const others = otherRe ? [...new Set(text.toLowerCase().match(otherRe) || [])].filter(n => n !== low) : [];
  const label = b => norm(b.innerText || b.value || b.getAttribute('aria-label') || '');
  const control = el => el && el.closest ? el.closest('button, [role=button], a, input[type=submit], input[type=button]') : null;
  const isSubmit = el => { const c = control(el); return !!c && SUBMITISH.test(label(c)); };
  const act = document.activeElement;
  // Enter in a plain text box submits its form (implicit submission).
  const implicit = !!act && act.tagName === 'INPUT' && /^(text|email|tel|number|url|password)$/i.test(act.type) &&
    !!act.form && act.getAttribute('role') !== 'combobox' && !act.getAttribute('aria-autocomplete');
  let hitSubmit = false;
  if (pt) hitSubmit = isSubmit(document.elementFromPoint(pt[0], pt[1]));
  return JSON.stringify({
    external: true, url: location.href, slots: slots.slice(0, 8),
    shown_ours: shown, ours_in_input: oursInInput, other_ours: others.slice(0, 3),
    active_is_submit: isSubmit(act) || implicit, point_hits_submit: hitSubmit,
  });
}"""

_SUBMIT_NAME = re.compile(r"\bsubmit\b", re.I)
# Any site's final button: "Submit", "Apply For This Job" (TurboHire 1635),
# "Send application", "Finish". "Apply now" on a listing passes: it is only
# refused while a resume upload failed or the page has a resume field.
_EXTERNAL_SUBMIT_NAME = re.compile(r"\b(submit|apply|send|finish)\b", re.I)
_SCRIPT_SUBMIT = re.compile(r"submit", re.I)
_EXTERNAL_SCRIPT_SUBMIT = re.compile(r"submit|apply", re.I)
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


def judge_external(states: list[dict], expected: str, guard: dict | None) -> tuple[bool, str]:
    """(may_submit, reason) on a non-LinkedIn form.

    guard: the driver's state ({"resume_uploads_ok": n, "resume_upload_failed": detail}).
    Refuses only while this job's resume is unverified AND either an upload
    of it failed or a page shows a resume field, so a form that has no resume
    field at all is never held up.
    """
    if not expected:
        return True, "this job has no resume file of its own to send"
    g = guard or {}
    if int(g.get("resume_uploads_ok") or 0) > 0:
        return True, "this job's resume upload was verified"
    if g.get("verified"):  # Easy Apply kept this job's own card: no new upload
        return True, "LinkedIn Easy Apply has this job's resume selected"
    for st in states or []:
        if st.get("shown_ours") or st.get("ours_in_input"):
            return True, f"the page shows this job's own file {expected}"
    for st in states or []:
        if st.get("other_ours"):
            return False, (f"the page shows {', '.join(repr(n) for n in st['other_ours'])}, a resume "
                           f"uploaded for another job (or an older copy of this job's), not this job's file "
                           f"{expected!r}")
    failed = str(g.get("resume_upload_failed") or "").strip()
    if failed:
        return False, f"the upload of this job's resume was never verified (last UPLOAD_FAILED: {failed[:160]})"
    for st in states or []:
        for slot in st.get("slots") or []:
            return False, (f"this form has a resume field ({slot.get('how', 'slot')} "
                           f"{str(slot.get('desc') or '')[:60]!r}) and this job's resume was never uploaded")
    return True, "this form has no resume field"


def refusal_message_external(reason: str, expected: str) -> str:
    return (
        f"BLOCKED by jobo: Submit refused, resume not verified: {reason}. The user's rule: every "
        f"application carries THIS job's own resume ({expected or 'resume_upload_name'}), "
        "verified on the page before it is sent. Do this instead: go to the form's resume/CV "
        "field (use Back if you are past it) and end your turn with exactly\n"
        "NEED_UPLOAD: resume\n"
        "(add ` | <attribute>` to pick the slot). Submit once the pipeline replies UPLOAD_DONE. "
        "If the form has no place that takes the file and NEED_UPLOAD keeps failing, do NOT "
        "submit: print STATUS: stuck with DETAIL naming the resume field and the upload error."
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


def inspect_external(base: str, point: list | None, expected: str) -> list[dict]:
    """Resume slots / our file on every non-LinkedIn page and frame target."""
    out = []
    for t in _http_json(f"{base}/json/list"):
        if t.get("type") not in ("page", "iframe") or not t.get("webSocketDebuggerUrl"):
            continue
        url = t.get("url", "")
        if is_linkedin(url) or not url.startswith(("http://", "https://")):
            continue
        # A point is in the page's viewport; a frame's own coordinates differ.
        pt = point if t.get("type") == "page" else None
        expr = f"({EXTERNAL_INSPECT_JS})({json.dumps([pt, expected])})"
        raw = _ws_eval(t["webSocketDebuggerUrl"], expr)
        state = json.loads(raw) if isinstance(raw, str) else {"external": False}
        state["target_id"] = t.get("id", "")
        out.append(state)
    return out


def _guard_state() -> dict:
    path = os.environ.get(ENV_STATE, "").strip()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _verified() -> bool:
    return bool(_guard_state().get("verified"))


def seen_file(state_file: str) -> str:
    """Where the veto records a submit it allowed because the page showed this
    job's own file (GHO-78). Lives next to the driver's state file."""
    return os.path.join(os.path.dirname(state_file), "resume_seen.json") if state_file else ""


def _record_seen(expected: str, what: str, states: list[dict]) -> None:
    path = seen_file(os.environ.get(ENV_STATE, "").strip())
    if not path:
        return
    url = next((s.get("url", "") for s in states if s.get("shown_ours") or s.get("ours_in_input")), "")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upload_name": expected, "what": what, "url": url[:300]}, fh)
    except OSError as e:  # the submit still goes through; the audit flags it
        logger.warning("jobo resume guard could not record the page evidence: %s", e)


def read_seen(state_file: str, expected: str) -> dict | None:
    """The veto's record that a submit-like action went out on a page showing
    `expected` (this job's own file), or None."""
    path = seen_file(state_file)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and expected and data.get("upload_name") == expected else None


# --- the pre_tool_call veto ---------------------------------------------------------


def _submit_attempt(tool_name: str, args: dict, ref_name,
                    names: re.Pattern = _SUBMIT_NAME) -> tuple[bool, list | None, str]:
    """(gated, point, what): does this call try to press a Submit control?"""
    if tool_name == "browser_click":
        name = (ref_name(str(args.get("ref") or "").lstrip("@")) or "") if ref_name else ""
        return bool(names.search(name)), None, f"click on {name!r}"
    if tool_name == "browser_press":
        key = str(args.get("key") or "")
        return key in ("Enter", "NumpadEnter", " ", "Space"), None, f"key {key}"
    script = _SCRIPT_SUBMIT if names is _SUBMIT_NAME else _EXTERNAL_SCRIPT_SUBMIT
    if tool_name == "browser_console":
        expr = str(args.get("expression") or "")
        return bool(script.search(expr) and _SCRIPT_ACTS.search(expr)), None, "script"
    if tool_name == "browser_cdp":
        method = str(args.get("method") or "").lower()
        raw = args.get("params")
        params: dict = raw if isinstance(raw, dict) else {}
        if method in ("runtime.evaluate", "runtime.callfunctionon"):
            src = str(params.get("expression") or params.get("functionDeclaration") or "")
            return bool(script.search(src) and _SCRIPT_ACTS.search(src)), None, "script"
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
    base = _cdp_base()
    if not base:
        return None
    gated, point, what = _submit_attempt(tool_name or "", a, ref_name)
    if gated:
        try:
            states = inspect_pages(base, point)
        except Exception as e:  # fail open: the driver's NEED_CLICK gate still applies
            logger.warning("jobo resume guard could not read the page: %s", e)
            states = []
        verified = _verified()
        for state in states:
            if not state.get("modal") or not _pressed(state, tool_name, point, what):
                continue
            ok, reason = judge(state, expected, verified)
            if not ok:
                logger.warning("jobo resume guard blocked %s (%s): %s", tool_name, what, reason)
                return refusal_message(reason, expected)
    return _check_external(tool_name or "", a, ref_name, base, expected)


def _pressed(state: dict, tool_name: str, point, what: str) -> bool:
    """Does a key/point action actually land on this state's Submit?"""
    if tool_name == "browser_press" and not state.get("active_is_submit"):
        return False
    if point is not None and not state.get("point_hits_submit"):
        return False
    if tool_name == "browser_cdp" and what == "key event" and not state.get("active_is_submit"):
        return False
    return True


def _check_external(tool_name: str, args: dict, ref_name, base: str, expected: str) -> str | None:
    """The same rule as the driver's NEED_CLICK gate, for every other site (GHO-70)."""
    gated, point, what = _submit_attempt(tool_name, args, ref_name, _EXTERNAL_SUBMIT_NAME)
    if not gated:
        return None
    guard = _guard_state()
    if int(guard.get("resume_uploads_ok") or 0) > 0 or guard.get("verified"):
        return None  # verified upload/selection: no page read needed
    try:
        states = [s for s in inspect_external(base, point, expected) if s.get("external")]
    except Exception as e:  # fail open: the driver's NEED_CLICK gate still applies
        logger.warning("jobo resume guard could not read the form: %s", e)
        return None
    # A key or a raw mouse press only counts when it lands on a submit control.
    if tool_name == "browser_press" or point is not None or what == "key event":
        if not any(_pressed(s, tool_name, point, what) for s in states):
            return None
    ok, reason = judge_external(states, expected, guard)
    if ok:
        if any(s.get("shown_ours") or s.get("ours_in_input") for s in states):
            _record_seen(expected, what, states)
        return None
    logger.warning("jobo resume guard blocked %s (%s): %s", tool_name, what, reason)
    return refusal_message_external(reason, expected)
