"""Deterministic NEED_CLICK handoff for submission and required consent.

Resolve one unambiguous logical control, find an unobstructed hit-test point,
then send at most one trusted CDP mouse press/release. Never remove overlays,
fabricate DOM events, set checked state, or retry an uncertain submission.
Checkbox/radio requests mean ensure-checked, with settled DOM read-back even
when already checked. Submission evidence is compared with a pre-click baseline.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import websocket  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

WS_TIMEOUT_SEC = 25
SETTLE_SEC = 1.2

# Shared read-only helpers also run in the new document after a navigation.
_DOM_JS = r"""
  const norm = s => String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
  const roots = () => {
    const all = [document];
    for (let i = 0; i < all.length; i++) {
      all[i].querySelectorAll('*').forEach(el => {
        if (el.shadowRoot) all.push(el.shadowRoot);
      });
    }
    return all;
  };
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.checkVisibility({
      checkOpacity: true, checkVisibilityCSS: true
    });
  };
  const parent = el => el.parentElement || el.getRootNode().host;
  const disabled = el => {
    if (el.matches(':disabled')) return true;
    for (let p = el; p; p = parent(p)) {
      if (p.inert || p.getAttribute('aria-disabled') === 'true') return true;
    }
    return false;
  };
  const labels = el => {
    const found = [...(el.labels || [])];
    const root = el.getRootNode();
    if (el.id) root.querySelectorAll('label[for]').forEach(l => {
      if (l.htmlFor === el.id && !found.includes(l)) found.push(l);
    });
    const wrap = el.closest('label');
    if (wrap && !found.includes(wrap)) found.push(wrap);
    return found;
  };
  // Avature consent boxes have an EMPTY <label for>; their question is the
  // legend of a fieldset holding only that one box (Wingtip 1318: four
  // NEED_CLICKs on the Privacy Policy consent all "matched nothing").
  const soleLegend = el => {
    if (!el.matches('input[type=checkbox], input[type=radio]')) return '';
    const fs = el.closest('fieldset');
    const lg = fs && fs.querySelector(':scope > legend');
    if (!lg || fs.querySelectorAll('input[type=checkbox], input[type=radio]').length !== 1) return '';
    return (lg.innerText || lg.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const label = el => {
    const refs = (el.getAttribute('aria-labelledby') || '').trim().split(/\s+/);
    const referenced = refs.map(id => el.getRootNode().getElementById(id))
      .filter(Boolean).map(n => n.textContent.trim()).join(' ');
    return referenced || el.getAttribute('aria-label') ||
      labels(el).map(l => l.innerText || l.textContent).join(' ').trim() ||
      soleLegend(el) ||
      (el.matches('input')
        ? (['submit', 'button'].includes(el.type) ? el.value : '') : el.innerText) ||
      el.getAttribute('title') || el.getAttribute('name') || '';
  };
  const SUCCESS = ['application submitted', 'thank you for applying',
    'application received', 'successfully submitted', 'application sent',
    'we have received your application', 'thanks for applying'];
  // Workday prints every field error as "Error: Please enter ..." under the
  // field (GHO-21: "Error: Passwords do not match" was reported as unknown).
  const ERRORS = ['this field is required', 'please complete', 'required field',
    'please fill', 'is required', 'error:', 'please enter'];
  const pageState = () => {
    const body = norm(roots().map(root => root === document
      ? (document.body?.innerText || '')
      : [...root.children].filter(visible).map(el => el.innerText || '').join(' ')
    ).join(' '));
    const counts = phrases => Object.fromEntries(phrases.map(p =>
      [p, body.split(p).length - 1]));
    return {url: location.href, success: counts(SUCCESS), errors: counts(ERRORS)};
  };
"""

# Return a remote object with closures, not globals or page DOM state. Keeping
# the original node/root also lets verification distinguish a remount from an
# unrelated checkbox. All JavaScript is discovery, hit testing, or observation.
_CLICK_JS = r"""
(function(hint) {
""" + _DOM_JS + r"""
  const want = norm(hint);
  const out = {ok: false, matched: '', strategy: '', note: '', candidates: [],
    blocked_by: '', disabled: false, before_url: location.href,
    after_url: '', changed: '', control_kind: '', checked: null};
  const nativeCheck = 'input[type=checkbox], input[type=radio]';
  const collect = () => {
    const found = [];
    for (const root of roots()) root.querySelectorAll(
      'button, input[type=submit], input[type=button], ' + nativeCheck +
      ', [role=button], [role=checkbox], [role=radio]'
    ).forEach(el => {
      // Styled widgets often expose both a role wrapper and a backing input.
      // They are one logical control, not two ambiguous candidates.
      if (!el.matches('input') && el.matches('[role=checkbox], [role=radio]') &&
          el.querySelector(nativeCheck)) return;
      const kind = el.matches(nativeCheck) ? el.type :
        (['checkbox', 'radio'].includes(el.getAttribute('role'))
          ? el.getAttribute('role') : 'button');
      let wrapper = el.matches(nativeCheck)
        ? el.closest('[role=checkbox], [role=radio]') : null;
      if (wrapper && wrapper.querySelectorAll(nativeCheck).length !== 1) wrapper = null;
      const name = label(el) || (wrapper ? label(wrapper) : '');
      const activatingLabels = labels(el).filter(l => l.control === el);
      const surfaces = [...new Set([el, ...activatingLabels, ...(wrapper ? [wrapper] : [])])]
        .filter(visible);
      if (name && surfaces.length) found.push({el, kind, label: name.trim(),
        labelN: norm(name), surfaces, root, form: el.form || null,
        id: el.id, name: el.getAttribute('name') || '', disabled: disabled(el)});
    });
    // Workday draws every action button (Create Account, Sign In, Submit, ...)
    // as an aria-hidden native <button> under a transparent sibling overlay,
    // div[data-automation-id=click_filter][role=button][aria-label=<same>].
    // Only the overlay handles the click; a click on the button itself does
    // nothing. They are ONE logical control: keep the overlay, drop the hidden
    // twin (GHO-14: "Create Account" was refused as ambiguous).
    const hiddenFromAT = el => !!el.closest('[aria-hidden=true]');
    return found.filter(n => !(hiddenFromAT(n.el) && found.some(o => o !== n &&
      !hiddenFromAT(o.el) && o.labelN === n.labelN &&
      o.el.parentElement === n.el.parentElement)));
  };
  const nodes = collect();
  out.candidates = nodes.slice(0, 25).map(n =>
    n.label.slice(0, 100) + (n.disabled ? ' [disabled]' : ''));
  const matching = nodes => {
    if (want) {
      const exact = nodes.filter(n => n.labelN === want);
      return exact.length ? exact : nodes.filter(n => n.labelN.includes(want));
    }
    const submitty = /^(submit|submit application|apply|apply now|send application|finish|complete)$/;
    return nodes.filter(n => n.kind === 'button' && !n.disabled &&
      ((n.el.type === 'submit' && n.el.form) || submitty.test(n.labelN)));
  };
  const matches = matching(nodes);
  let target = matches.length === 1 ? matches[0] : null;
  if (!target) {
    out.note = matches.length ? 'ambiguous control hint; no click sent' : want
      ? 'no reachable control matched ' + JSON.stringify(hint) + '; no click sent'
      : 'no unambiguous enabled submit control found; no click sent';
  }
  const isCheck = target && target.kind !== 'button';
  const sameIdentity = n => n.root === target.root && n.form === target.form &&
    (!target.form || target.form.isConnected) && n.kind === target.kind &&
    n.labelN === target.labelN && n.id === target.id && n.name === target.name;
  const readChecked = el => el.matches(nativeCheck)
    ? el.checked && !el.indeterminate : el.getAttribute('aria-checked') === 'true';
  let listening = false;
  // What the page saw of OUR click on a check control (GHO-23): Avature
  // consent boxes on 2026-10-15 never checked under any trusted input and
  // never even took focus, and nothing said whether the click was lost before
  // the page, cancelled by it, or undone afterwards.
  const seen = [];
  const record = event => {
    if (event.target === target?.el || target?.surfaces.includes(event.target) ||
        target?.el.contains?.(event.target)) seen.push(event);
  };
  const SEEN_TYPES = ['pointerdown', 'mousedown', 'click'];
  let watching = false;
  const invalid = new Set();
  const onInvalid = event => {
    if (target?.el.form && event.target.form === target.el.form)
      invalid.add(label(event.target) || event.target.id || 'required field');
  };
  // Observation only: did the page itself see our trusted click land on the
  // control? Separates "input never arrived" from "the site ignored it".
  let received = false;
  const onClick = event => {
    if (event.isTrusted && target?.surfaces.some(s => s === event.target || s.contains(event.target)))
      received = true;
  };
  const cleanup = () => {
    if (listening) for (const root of roots()) {
      root.removeEventListener('invalid', onInvalid, true);
      root.removeEventListener('click', onClick, true);
    }
    listening = false;
    if (watching) SEEN_TYPES.forEach(t => window.removeEventListener(t, record, true));
    watching = false;
    return null;
  };
  const checkDisabled = () => {
    if (!disabled(target.el)) return false;
    out.disabled = true;
    const missing = [];
    for (const root of roots()) root.querySelectorAll('[required], [aria-required=true]')
      .forEach(el => {
        if (el.matches(':disabled')) return;
        const check = el.matches(nativeCheck + ', [role=checkbox], [role=radio]');
        const empty = check ? !readChecked(el) : !String(el.value || '').trim();
        if (empty) missing.push((label(el) || el.id || '?').slice(0, 80));
      });
    out.note = 'the control is DISABLED, so no click was sent.' +
      (missing.length ? ' Required fields still empty: ' + missing.slice(0, 10).join(' / ') : '');
    return true;
  };
  const hit = (x, y) => {
    let top = document.elementFromPoint(x, y);
    while (top?.shadowRoot) {
      const deep = top.shadowRoot.elementFromPoint(x, y);
      if (!deep || deep === top) break;
      top = deep;
    }
    return top;
  };
  const point = () => {
    for (const surface of target.surfaces.filter(visible)) {
      const r = surface.getBoundingClientRect();
      for (const [fx, fy] of [[.5,.5],[.15,.5],[.85,.5],[.5,.15],[.5,.85]]) {
        const x = r.left + r.width * fx, y = r.top + r.height * fy;
        const top = hit(x, y);
        if (top && (top === surface || surface.contains(top))) {
          // A label can contain a terms hyperlink or a different control.
          // Clicking that descendant does NOT reliably select its input.
          const interactive = top.closest('a, button, input, select, textarea, ' +
            '[role=button], [role=checkbox], [role=radio]');
          if (!interactive || interactive === target.el || interactive === surface &&
              surface.matches('[role=checkbox], [role=radio]')) return {x, y};
        }
        if (!out.blocked_by) out.blocked_by = top
          ? top.tagName + (top.id ? '#' + top.id : '') : 'outside viewport';
      }
    }
    out.note = 'intended control is obscured or has no safe hit-test point; no click sent. ' +
      'No overlay or dialog was modified.';
    return null;
  };
  const ready = () => {
    if (!target || out.note) return {...out, ready: false};
    if (!target.el.isConnected || norm(label(target.el) || target.label) !== target.labelN) {
      out.note = 'control changed before the click; take a fresh snapshot; no click sent';
      return {...out, ready: false};
    }
    if (checkDisabled()) return {...out, ready: false};
    const current = matching(collect());
    if (current.length !== 1 || current[0].el !== target.el || !sameIdentity(current[0])) {
      out.note = 'control became ambiguous or changed before the click; no click sent';
      return {...out, ready: false};
    }
    if (isCheck && readChecked(target.el))
      return {...out, ready: true, already_checked: true};
    const xy = point();
    if (!xy) return {...out, ready: false};
    out.blocked_by = '';
    if (!listening) {
      for (const root of roots()) {
        root.addEventListener('invalid', onInvalid, true);
        root.addEventListener('click', onClick, true);
      }
      listening = true;
    }
    if (isCheck && !watching) {
      SEEN_TYPES.forEach(t => window.addEventListener(t, record, true));
      watching = true;
    }
    return {...out, ready: true, already_checked: false, ...xy};
  };
  const prepare = async () => {
    if (!target) return {...out, ready: false};
    out.matched = target.label;
    out.control_kind = target.kind;
    if (checkDisabled()) return {...out, ready: false};
    if (!(isCheck && readChecked(target.el))) {
      target.surfaces[0].scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
      await new Promise(r => setTimeout(r, 120));
    }
    return ready();
  };
  const observe = () => {
    let current = target?.el;
    if (isCheck) {
      const replacements = collect().filter(sameIdentity);
      current = replacements.length === 1 &&
        (!current.isConnected || replacements[0].el === current) ? replacements[0].el : null;
    }
    const clicks = seen.filter(e => e.type === 'click');
    return {checked: isCheck && current?.isConnected ? readChecked(current) : null,
      gone: !target?.el.isConnected, disabled: current ? disabled(current) : false,
      invalid: [...invalid], received,
      trusted_events: seen.filter(e => e.isTrusted).map(e => e.type),
      click_cancelled: clicks.some(e => e.defaultPrevented),
      page_focused: document.hasFocus(), visibility: document.visibilityState};
  };
  // Fallback for a native checkbox/radio that a trusted click left unchecked:
  // the element's own click() runs the browser's activation behaviour (toggle
  // plus input/change events) without going through the input pipeline. Only
  // ever on the exact, still-unchecked, enabled box: never a button, so it
  // cannot submit anything.
  const domClick = () => {
    const el = target?.el;
    if (!isCheck || !el?.isConnected || !el.matches(nativeCheck) || disabled(el) ||
        readChecked(el)) return false;
    seen.length = 0;
    el.click();
    return true;
  };
  return {prepare, ready, observe, cleanup, domClick};
})
"""
_PAGE_STATE_JS = "(() => {" + _DOM_JS + "return pageState();})()"


@dataclass
class ClickResult:
    ok: bool
    hint: str = ""
    matched: str = ""
    strategy: str = ""
    note: str = ""
    blocked_by: str = ""
    disabled: bool = False
    changed: str = ""
    before_url: str = ""
    after_url: str = ""
    candidates: list[str] = field(default_factory=list)
    control_kind: str = ""
    checked: Optional[bool] = None
    delivered: bool = False
    outcome: str = "refused"
    received: Optional[bool] = None
    # Same-site requests the page sent after the press: "POST /path -> 403".
    requests: list[str] = field(default_factory=list)

    def as_agent_message(self) -> str:
        name = self.matched or self.hint
        if self.control_kind in {"checkbox", "radio"}:
            if self.ok:
                return (f"CLICKED: {name} (via {self.strategy})\n"
                        f"{self.changed}. Checked state verified after settling. "
                        "Continue filling the remaining fields. Do not toggle this "
                        "control again. This is not a submission verdict.")
            if self.delivered or self.strategy == "already-checked":
                return (f"CLICK_FAILED: {self.note}\nThe control did not remain "
                        "checked after settling. Take a fresh snapshot and inspect "
                        "the field or its validation. Do not blindly toggle it again.")
        if self.delivered:
            if self.outcome == "rejected":
                return (f"CLICK_FAILED: {self.note}\nThe click reached {name} and the "
                        "page sent its request, but the site's server refused it. The "
                        "page shows no error for this. Clicking again sends the same "
                        "refused request. Report this exact reason if nothing else "
                        "on the page can get past it.")
            if self.outcome == "validation":
                return (f"CLICK_FAILED: {self.changed}\nThe click was sent once, "
                        "but new validation blocked progress. Fix the named fields "
                        "(NEED_FIELDS for numbers), then submit once after fixing them.")
            return (f"CLICKED: {name} (via {self.strategy or 'CDP mouse'})\n"
                    + (f"What changed: {self.changed}\n" if self.changed else "")
                    + (self.note + "\n" if self.note else "")
                    + "Submission is unconfirmed until a fresh snapshot shows a "
                    "confirmation screen or message. Wait for any pending response "
                    "and inspect the page. Do NOT click again or request NEED_CLICK "
                    "again while the outcome is unknown; that can duplicate submission.")
        lines = [f"CLICK_FAILED: {self.note or 'the click did not work'}"]
        if self.disabled:
            lines.append("Fix the cause, not the click: fill the fields named above "
                         "(use NEED_FIELDS to get their numbers), then ask for the click again.")
        elif self.blocked_by:
            lines.append(f"Something was covering the control ({self.blocked_by}). "
                         "Inspect the dialog or overlay. Dismiss it only if appropriate, "
                         "then take a fresh snapshot. It was not bypassed.")
        elif self.candidates:
            lines.append("Reachable candidate labels (duplicates are ambiguous):")
            lines.extend(f"  - {c}" for c in self.candidates[:12])
            lines.append("Use a unique exact label with NEED_CLICK. If labels are "
                         "identical, resolve the page ambiguity first, not by guessing.")
        return "\n".join(lines)


class _Cdp:
    def __init__(self, ws_url: str):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed")
        self.ws = websocket.create_connection(
            ws_url, timeout=WS_TIMEOUT_SEC, suppress_origin=True
        )
        self._id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method", "").startswith("Network."):
                self.events.append(msg)
            elif msg.get("id") == self._id:
                if msg.get("error"):
                    raise RuntimeError(f"{method}: {msg['error']}")
                if msg.get("result", {}).get("exceptionDetails"):
                    raise RuntimeError(f"{method}: JavaScript evaluation failed")
                return msg

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _page_targets(cdp_url: str) -> list[dict]:
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=5) as r:
            tabs = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("click: could not list targets: %s", e)
        return []
    return [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def _value(response: dict):
    return response["result"]["result"]["value"]


def _snapshot(cdp: _Cdp) -> dict:
    return _value(cdp.call("Runtime.evaluate", {
        "expression": _PAGE_STATE_JS, "returnByValue": True,
    }))


def _site(host: str) -> str:
    return ".".join(host.split(".")[-2:])


def _site_requests(events: list[dict], page_url: str) -> list[tuple[str, Optional[int], str]]:
    """(METHOD /path, HTTP status or None, failure) for every same-site request
    the page sent after the press, skipping plain GETs of assets and data."""
    site = _site(urllib.parse.urlsplit(page_url).hostname or "")
    seen: dict[str, list] = {}
    for ev in events:
        p = ev.get("params", {})
        rid = p.get("requestId")
        if ev["method"] == "Network.requestWillBeSent":
            req = p.get("request", {})
            url = urllib.parse.urlsplit(req.get("url", ""))
            if _site(url.hostname or "") == site and (
                    req.get("method") != "GET" or p.get("type") == "Document"):
                seen[rid] = [f"{req.get('method')} {url.path}", None, ""]
        elif rid in seen and ev["method"] == "Network.responseReceived":
            seen[rid][1] = p.get("response", {}).get("status")
        elif rid in seen and ev["method"] == "Network.loadingFailed":
            seen[rid][2] = p.get("blockedReason") or p.get("errorText") or "failed"
    return [tuple(v) for v in seen.values()]


def _on_plan(cdp: _Cdp, object_id: str, method: str):
    return _value(cdp.call("Runtime.callFunctionOn", {
        "objectId": object_id, "functionDeclaration": f"function() {{ return this.{method}(); }}",
        "returnByValue": True, "awaitPromise": True,
    }))


def _lost_click_reason(seen: dict, trusted: bool = True) -> str:
    """Say where a click on a check control was lost, from observe()'s record.

    trusted=False describes the element's own click(), whose events are never
    trusted but are always delivered.
    """
    focus = ("" if seen.get("page_focused") and seen.get("visibility") == "visible" else
             f" (page focused={seen.get('page_focused')}, {seen.get('visibility')})")
    if seen.get("click_cancelled"):
        return "the page cancelled the click (preventDefault)" + focus
    if seen.get("checked") is None:
        return "the control was replaced or could not be reidentified" + focus
    if not trusted:
        return "the box was unchecked again" + focus
    if not seen.get("trusted_events"):
        return "no trusted click reached the page" + focus
    if "click" in (seen.get("trusted_events") or []):
        return "the click reached the page but the box was unchecked again" + focus
    return "only " + "/".join(seen["trusted_events"]) + " reached the page, no click" + focus


def click(cdp_url: str, hint: str = "") -> ClickResult:
    """Use only the first active page target, never another tab as a fallback.

    `ok` means settled selection or new positive page evidence, not merely a
    sent gesture. `delivered` remains true on uncertain delivery so the agent
    does not retry a potentially submitted application.
    """
    from module4 import form_filler

    alive, note = form_filler._ensure_browser(cdp_url)
    if not alive:
        return ClickResult(ok=False, hint=hint, note=note)
    targets = _page_targets(cdp_url)
    if not targets:
        return ClickResult(ok=False, hint=hint, note="no page target")

    res = ClickResult(ok=False, hint=hint)
    try:
        with _Cdp(targets[0]["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Runtime.enable")
            plan = cdp.call("Runtime.evaluate", {
                "expression": f"({_CLICK_JS})({json.dumps(hint)})",
                "returnByValue": False,
            })["result"]["result"]["objectId"]
            try:
                data = _on_plan(cdp, plan, "prepare")
                for name in ("matched", "note", "candidates", "disabled", "blocked_by",
                             "before_url", "control_kind"):
                    setattr(res, name, data[name])
                if not data["ready"]:
                    return res
                if not data["already_checked"]:
                    cdp.call("Input.dispatchMouseEvent", {
                        "type": "mouseMoved", "x": data["x"], "y": data["y"],
                    })
                before = _snapshot(cdp)
                # Snapshot may run in a new document. Revalidate the ORIGINAL
                # remote plan after it; a lost context must fail before any press.
                ready = _on_plan(cdp, plan, "ready")
                if not ready["ready"]:
                    res.note, res.blocked_by = ready["note"], ready["blocked_by"]
                    res.disabled = ready["disabled"]
                    return res
                if not ready["already_checked"] and (
                    data["already_checked"] or
                    ready["x"] != data["x"] or ready["y"] != data["y"]
                ):
                    res.note = "control moved or changed before press; no click sent; take a fresh snapshot"
                    return res
                data = ready
                res.before_url = before["url"]
                if data["already_checked"]:
                    res.strategy = "already-checked"
                else:
                    res.strategy = "cdp-mouse"
                    # Watch what the page sends after the press (GHO-21: a
                    # Workday Create Account whose request never left, or was
                    # refused, looked exactly like a click that did nothing).
                    cdp.call("Network.enable")
                    cdp.events.clear()
                    # Mark uncertainty BEFORE sending: transport failure is not proof
                    # that the site did not receive the gesture. Never resend it.
                    res.delivered = True
                    mouse = {"x": data["x"], "y": data["y"], "button": "left", "clickCount": 1}
                    try:
                        cdp.call("Input.dispatchMouseEvent", {**mouse, "type": "mousePressed"})
                    finally:
                        cdp.call("Input.dispatchMouseEvent", {**mouse, "type": "mouseReleased"})
                time.sleep(SETTLE_SEC)
                after = _snapshot(cdp)
                res.after_url = after["url"]
                is_check = res.control_kind in {"checkbox", "radio"}
                if after["url"] != before["url"]:
                    if is_check:
                        res.note = "page navigated; committed checked state could not be verified"
                    else:
                        res.ok, res.outcome = True, "navigation"
                        res.changed = "the page navigated"
                    return res
                observed = _on_plan(cdp, plan, "observe")
                if is_check:
                    # Require a second stable sample, not a transient pre-render tick.
                    time.sleep(0.15)
                    settled = _on_plan(cdp, plan, "observe")
                    res.checked = settled["checked"]
                    res.ok = observed["checked"] is True and res.checked is True
                    why = "" if res.ok or not res.delivered else _lost_click_reason(settled)
                    # Only for a click that never reached the page: one the page
                    # received and undid or cancelled is the page's answer.
                    fallback = (bool(why) and "click" not in settled["trusted_events"]
                                and not settled["click_cancelled"])
                    tried = fallback and _on_plan(cdp, plan, "domClick")
                    if tried:
                        logger.info("click: %r did not take a trusted click (%s); "
                                    "using the element's own click()", res.matched, why)
                        time.sleep(SETTLE_SEC)
                        observed = _on_plan(cdp, plan, "observe")
                        time.sleep(0.15)
                        settled = _on_plan(cdp, plan, "observe")
                        res.checked = settled["checked"]
                        res.ok = observed["checked"] is True and res.checked is True
                        if res.ok:
                            res.strategy = "cdp-mouse+dom-click"
                    res.outcome = "checked" if res.ok else "not-checked"
                    if res.ok:
                        res.changed = ("already checked; no click sent" if not res.delivered
                                       else "the requested control is checked")
                    else:
                        res.note = "requested control did not stay checked after settling (or could not be uniquely reidentified)"
                        if why:
                            res.note += f"; the trusted click: {why}"
                        if tried:
                            res.note += ("; the element's own click(): "
                                         + _lost_click_reason(settled, trusted=False))
                    return res
                success = [p for p, count in after["success"].items()
                           if count > before["success"].get(p, 0)]
                errors = [p for p, count in after["errors"].items()
                          if count > before["errors"].get(p, 0)]
                if errors or observed["invalid"]:
                    res.outcome = "validation"
                    res.changed = "new validation: " + "; ".join(observed["invalid"] + errors)
                    res.note = "new validation blocked progress; fix it before retrying"
                elif success:
                    res.ok, res.outcome = True, "confirmation"
                    res.changed = 'new confirmation text: "' + success[0] + '"'
                else:
                    changes = []
                    if observed["gone"]:
                        changes.append("the control was removed")
                    elif observed["disabled"]:
                        changes.append("the control became disabled")
                    res.changed = "; ".join(changes)
                    res.outcome = "unknown"
                    res.received = observed["received"]
                    sent = _site_requests(cdp.events, before["url"])
                    res.requests = [f"{r} -> {status or failed or 'no response yet'}"
                                    for r, status, failed in sent]
                    refused = [line for line, (_, status, failed) in zip(res.requests, sent)
                               if (status or 0) >= 400 or failed and not status]
                    if refused:
                        res.outcome = "rejected"
                        res.note = "the site refused the request: " + "; ".join(refused[:3])
                    else:
                        if not res.received:
                            seen = "the page never received the click event (browser input problem, not the site)"
                        elif not sent and not changes:
                            seen = "the page received the click but sent no request and nothing changed"
                        else:
                            seen = "requests: " + "; ".join(res.requests[:3]) if sent else "the page received the click"
                        res.note = (seen + ". One click sent; submission unconfirmed after the observation "
                                    "window. An async response may still be pending; do not retry the click.")
            finally:
                try:
                    _on_plan(cdp, plan, "cleanup")
                    cdp.call("Runtime.releaseObject", {"objectId": plan})
                except Exception:
                    pass  # Navigation destroys the old object and its listeners.
    except Exception as e:
        logger.warning("click failed: %s", e)
        res.ok = False
        res.outcome = "unknown" if res.delivered else "refused"
        res.note = (("gesture may have been delivered; submission unconfirmed; do not retry. "
                     if res.delivered else "no click sent. ") + str(e)[:140])
    return res


# Label optional: blank requests select only an unambiguous enabled submit.
_REQUEST_RE = re.compile(r"^NEED_CLICK:?[ \t]*(?P<label>[^\r\n]*)$", re.MULTILINE | re.I)


def parse_request(text: str) -> Optional[str]:
    m = _REQUEST_RE.search(text)
    return m.group("label").strip() if m else None
