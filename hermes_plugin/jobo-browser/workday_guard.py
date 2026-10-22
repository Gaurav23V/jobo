"""Which field is still invalid, and no Save and Continue before it is fixed (GHO-72).

Reputation 1464 and Verizon 2219 (run_20261021_2350) each pressed Workday's
Save and Continue with one required field empty ("How Did You Hear About
Us?", the Race/Ethnicity boxes). Workday showed "Errors Found" and disabled
the button; it re-enables it from an animation frame, which a browser on a
locked screen never draws (module4/frame_keeper.py). The agents then spent
their whole budget re-selecting values that were already committed, because
nothing told them what Workday still considered invalid, and our own
"Required fields still empty" list named fields that were filled:

  * a Workday radio group is <div aria-required=true name=...>, no value;
  * a Workday search prompt keeps value="" on its input; the picks are
    [data-automation-id=selectedItem] chips beside it;
  * a multi-checkbox question (Race/Ethnicity) marks every box required,
    and one checked box answers it.

Real markup, saved read-only from the live Reputation form on 2026-10-22:

    <div data-automation-id="errorHeading"><ol><li>
      <button><span>Error</span><div aria-hidden="true">-</div>How Did You Hear About Us?</button>
      <div id="hint9">The field How Did You Hear About Us? is required and must have a value.</div>
    </li></ol></div>
    <div data-automation-id="formField-source"><label for="source--source">How Did You Hear About Us?*</label>
      <input id="source--source" data-uxi-widget-type="selectinput" aria-required="true" aria-invalid="true" value="">
      <p data-automation-id="inputAlert">Error: The field ... is required and must have a value.</p></div>
    <div name="candidateIsPreviousWorker" aria-labelledby="radio-label7" aria-required="true"> ...radios... </div>
    <button data-automation-id="pageFooterNextButton">Save and Continue</button>

INSPECT_JS reads that: the footer button, Workday's own error list and
aria-invalid fields, and the required fields that are really unanswered
(REQUIRED_JS, also used by module4/clicker.py on any site).
check_tool_call() is the Hermes pre_tool_call veto: a browser_click on
Workday's Save and Continue / Next / Submit while a required field is
unanswered is BLOCKED (twice at most for the same page and fields, so a
reading we get wrong cannot wall the form off).
module4/clicker.py and module4/form_filler.py load this file by path, so the
driver's messages and the plugin agree. Reading the page fails OPEN.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The body of a function: returns [{label, why}] for required fields that are
# really unanswered. Needs `norm` (whitespace-collapsed text) in scope.
REQUIRED_JS = r"""
  const reqVis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const CHECK = 'input[type=checkbox], input[type=radio], [role=checkbox], [role=radio]';
  const isChecked = el => el.matches('input') ? el.checked : el.getAttribute('aria-checked') === 'true';
  const reqLabel = el => {
    const ff = el.closest('[data-automation-id^="formField-"]');
    const head = ff && ff.querySelector('legend, label');
    const ids = (el.getAttribute('aria-labelledby') || '').trim().split(/\s+/).filter(Boolean);
    const byIds = ids.map(id => document.getElementById(id)).filter(Boolean)
      .map(n => n.innerText || n.textContent).join(' ');
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    return norm((head && head.innerText) || byIds ||
      (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') ||
      (legend && legend.innerText) || el.name || el.id || '').replace(/\s*\*$/, '');
  };
  // The question a check control answers, and every box/button in it.
  const checkGroup = el => {
    if (el.matches('input[type=radio]') && el.name) {
      const scope = el.form || el.getRootNode();
      const same = [...scope.querySelectorAll('input[type=radio]')].filter(r => r.name === el.name);
      if (same.length > 1) return same;
    }
    const box = el.closest('fieldset, [role=radiogroup], [role=group], [role=grid], [data-automation-id^="formField-"]');
    const members = box ? [...box.querySelectorAll(CHECK)] : [];
    return members.length ? members : [el];
  };
  const unanswered = [];
  const seenReq = new Set();
  const flag = (el, why) => {
    const label = reqLabel(el) || '(unlabelled)';
    if (seenReq.has(label)) return;
    seenReq.add(label);
    unanswered.push({label: label.slice(0, 90), why});
  };
  document.querySelectorAll('[required], [aria-required=true]').forEach(el => {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('[aria-hidden=true]')) return;
    if (el.matches(CHECK)) {
      if (!checkGroup(el).some(isChecked)) flag(el, 'nothing checked');
      return;
    }
    // A radio/checkbox group wrapper (Workday: <div aria-required name=...>).
    const inner = el.matches('input, select, textarea, button') ? [] : [...el.querySelectorAll(CHECK)];
    if (inner.length) {
      if (!inner.some(isChecked)) flag(el, 'nothing checked');
      return;
    }
    if (el.matches('input[data-uxi-widget-type=selectinput]')) {
      const box = el.closest('[data-automation-id=multiSelectContainer], [data-automation-id=multiselectInputContainer]') || el.parentElement;
      const wrap = box && (box.closest('[data-automation-id=multiSelectContainer]') || box);
      if (!wrap.querySelector('[data-automation-id=selectedItem]')) flag(el, 'nothing selected');
      return;
    }
    if (el.matches('button[aria-haspopup=listbox]')) {
      const t = norm(el.innerText);
      if (!t || /^select one$/i.test(t)) flag(el, 'nothing selected');
      return;
    }
    if (el.matches('input, select, textarea')) {
      if (!reqVis(el)) return;  // a 0x0 store beside its real widget
      if (!String(el.value || '').trim()) flag(el, 'empty');
    }
    // Anything else marked required (a wrapper div) is judged by the control inside it.
  });
"""

# Runs in any page. Returns a JSON string (see judge()).
INSPECT_JS = r"""(() => {
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
""" + REQUIRED_JS + r"""
  const host = location.hostname;
  const footerBtn = document.querySelector('[data-automation-id=pageFooterNextButton]');
  const workday = /(^|\.)(myworkdayjobs|myworkday|workday)\.com$/i.test(host) || !!footerBtn;
  const errors = [];
  document.querySelectorAll('[data-automation-id=errorHeading] li').forEach(li => {
    const b = li.querySelector('button');
    const name = norm(b ? b.innerText : '').replace(/^error\s*-?\s*/i, '');
    const hint = norm([...li.querySelectorAll('div[id]')].map(d => d.innerText).join(' '));
    errors.push((name + (hint ? ': ' + hint : '')).slice(0, 200));
  });
  if (!errors.length) document.querySelectorAll('[data-automation-id=inputAlert]').forEach(p => {
    const ff = p.closest('[data-automation-id^="formField-"]');
    const lab = ff && ff.querySelector('legend, label');
    errors.push((norm(lab ? lab.innerText : '').replace(/\s*\*$/, '') + ': ' +
                 norm(p.innerText).replace(/^error:\s*/i, '')).slice(0, 200));
  });
  const invalid = [...new Set([...document.querySelectorAll('input[aria-invalid=true], select[aria-invalid=true], textarea[aria-invalid=true], button[aria-invalid=true], [role=radiogroup][aria-invalid=true], [role=combobox][aria-invalid=true]')]
    .map(reqLabel).filter(Boolean))].slice(0, 12);
  return JSON.stringify({
    url: location.href, workday,
    footer: footerBtn ? {label: norm(footerBtn.innerText), disabled: !!footerBtn.disabled ||
      footerBtn.getAttribute('aria-disabled') === 'true'} : null,
    site_errors: errors.slice(0, 12), invalid, missing: unanswered.slice(0, 15),
  });
})()"""

_FOOTER_NAME = re.compile(r"^(?:button\s+)?(save and continue|next|continue|submit|review)\b", re.I)


def is_workday(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return bool(re.search(r"(^|\.)(myworkdayjobs|myworkday|workday)\.com$", host))


def describe(state: dict | None) -> str:
    """One paragraph: what the site flags and what is really unanswered."""
    if not state:
        return ""
    bits = []
    if state.get("site_errors"):
        bits.append("The site's own error list: " + " / ".join(e.rstrip(". ") for e in state["site_errors"][:6]) + ".")
    elif state.get("invalid"):
        bits.append("Fields the site marks invalid: " + " / ".join(state["invalid"][:6]) + ".")
    missing = state.get("missing") or []
    if missing:
        bits.append("Required and really unanswered: " + " / ".join(
            f"{m['label']} ({m['why']})" for m in missing[:8]) + ".")
    return " ".join(bits)


def judge(state: dict | None, ref_name: str) -> tuple[bool, str]:
    """(may_click, reason) for a click on `ref_name` in this page state."""
    if not state or not state.get("workday"):
        return True, "not a Workday page"
    footer = state.get("footer") or {}
    name = re.sub(r"^button\s+", "", (ref_name or "").strip(), flags=re.I)
    if not footer or name.lower() != (footer.get("label") or "").lower():
        return True, "not Workday's page footer button"
    if footer.get("disabled"):
        return True, "the footer button is disabled; a click does nothing"
    missing = state.get("missing") or []
    if not missing:
        return True, "every required field is answered"
    return False, "required fields are still unanswered: " + " / ".join(
        f"{m['label']} ({m['why']})" for m in missing[:8])


def refusal_message(reason: str) -> str:
    return (
        f"BLOCKED by jobo: Workday's page button was not clicked: {reason}. Pressing Save "
        "and Continue with a required field empty makes Workday disable the button "
        "behind an \"Errors Found\" list, and the agent then re-selects values that "
        "were already fine. Fill exactly the fields named here (print NEED_FIELDS for "
        "their numbers; search prompts such as \"How Did You Hear About Us?\" need an "
        "option picked, typed text alone is not a value), then click once."
    )


# --- live page reading ----------------------------------------------------------


def _cdp_base() -> str | None:
    raw = os.environ.get("BROWSER_CDP_URL", "").strip()
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def _ws_eval(ws_url: str, expression: str, timeout: float = 4.0):
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
    result = reply.get("result") or {}
    if reply.get("error") or result.get("exceptionDetails"):
        raise RuntimeError("workday inspect failed")
    return (result.get("result") or {}).get("value")


def inspect_pages(base: str) -> list[dict]:
    """INSPECT_JS state of every Workday page target."""
    with urllib.request.urlopen(f"{base}/json/list", timeout=3) as resp:
        targets = json.loads(resp.read().decode("utf-8"))
    out = []
    for t in targets:
        if t.get("type") != "page" or not is_workday(t.get("url", "")) or not t.get("webSocketDebuggerUrl"):
            continue
        raw = _ws_eval(t["webSocketDebuggerUrl"], INSPECT_JS)
        if isinstance(raw, str):
            out.append(json.loads(raw))
    return out


# --- the pre_tool_call veto ---------------------------------------------------------

MAX_BLOCKS = 2
_blocks: dict[tuple, int] = {}


def check_tool_call(tool_name: str, args: dict | None, ref_name=None) -> str | None:
    """Block message, or None to let the call through."""
    if tool_name != "browser_click" or not os.environ.get("JOBO_UPLOAD_RESUME_NAME", "").strip():
        return None  # only inside a jobo apply run
    a = args if isinstance(args, dict) else {}
    name = (ref_name(str(a.get("ref") or "").lstrip("@")) or "") if ref_name else ""
    if not _FOOTER_NAME.search(name):
        return None
    base = _cdp_base()
    if not base:
        return None
    try:
        states = inspect_pages(base)
    except Exception as e:  # fail open
        logger.warning("jobo workday guard could not read the page: %s", e)
        return None
    for state in states:
        ok, reason = judge(state, name)
        if ok:
            continue
        key = (state.get("url", ""), tuple(m["label"] for m in state.get("missing") or []))
        _blocks[key] = _blocks.get(key, 0) + 1
        if _blocks[key] > MAX_BLOCKS:
            logger.warning("jobo workday guard: letting a click through after %d blocks: %s",
                           MAX_BLOCKS, reason)
            return None
        logger.warning("jobo workday guard blocked a click on %r: %s", name, reason)
        return refusal_message(reason)
    return None
