"""Deterministic dropdown/select setting for Module 4, performed driver-side.

Why this exists
---------------
After uploads were moved into code, dropdowns became the dominant remaining
failure. On the 2026-09-04 run, 4+ of the first 12 stuck jobs died on
select2-style autocomplete widgets (Avature "college name", Wingtip's "School",
SuccessFactors "Country/Region Code"), and they died the expensive way: the
agent burned its ENTIRE turn budget re-typing and re-clicking a single field.

One transcript names the root cause exactly:

    "...clicked the dropdown without registering the selection - the
     underlying hidden `<select id="10955">` never updated"

That is the whole problem. select2/chosen/react-select render a fake listbox
on top of a REAL hidden `<select>` (or hidden input). Clicking the pretty
widget is unreliable under automation, but setting the backing element's value
and firing the events the framework listens for is completely deterministic.

Same lesson as the uploader: a mechanical, fully-specified DOM operation should
not be delegated to an LLM. The agent names the field and the value it wants;
this module does the DOM work and verifies it stuck.

Strategy per field, in order:
  1. native `<select>`      -> set value by exact/fuzzy option-text match
  2. select2/chosen/etc.    -> set the backing hidden select, then notify the
                               widget (jQuery .trigger('change') when present,
                               plus native input/change events)
  3. text input/combobox    -> set value via the React-safe value setter and
                               fire input+change
Every path verifies by reading the value back out of the DOM afterwards, so a
reported success means the form actually holds the value.

Addressing fields by NUMBER, not by prose (added 2026-09-04)
------------------------------------------------------------
The first version took a natural-language hint and fuzzy-matched it against
DOM metadata. That produced the worst possible failure on a live run: the hint

    'field with JSON key "college" (visible label "College Name (required)",
     NOT the "Full name" input)'

matched the *Full name* input, wrote "Springfield Institute of Technology" into it, and reported
`ok=True`. The agent was told "set and verified, do not re-type". Worse, the
loop was self-amplifying: when the college field stayed empty the agent wrote
a LONGER, more descriptive hint, which diluted the token ratio and matched
even more badly. Nine rounds, each worse than the last.

The fix is to remove the matching problem rather than improve the matcher.
`list_fields()` enumerates the page's controls and gives each a stable number;
the agent then says `NEED_FIELD: #2 = Springfield Institute of Technology`. There is nothing to
interpret. Prose hints still work (the agent will sometimes use them anyway)
but are now held to a much stricter bar - see `_hint_is_confident`.

Index stability is enforced, not assumed: `list_fields` returns a signature
per control, and setting by index re-enumerates and refuses if the control at
that position is no longer the one that was listed (multi-step forms rewrite
the DOM between turns).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from module4 import browser_session

logger = logging.getLogger(__name__)

try:
    import websocket  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

WS_TIMEOUT_SEC = 20
# Extra searches a search box gets when the asked-for value finds nothing:
# the candidate's other spellings plus the two escape queries (~5s each).
MAX_SEARCH_FALLBACKS = 6


def _norm_py(s: str) -> str:
    """Same normalisation the in-page JS uses, for comparing values Python-side."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())

# Shared control-enumeration JS. Both `list_fields` (numbering) and
# `set_field` (resolution) walk the DOM the SAME way, so the number the agent
# is shown always refers to the control the setter will act on.
_COLLECT_JS = r"""
  const norm = s => (s || '').toString().trim().toLowerCase().replace(/\s+/g, ' ');
  // One accessible label resolver for listing, dispatch and identity guards.
  const accessibleLabel = el => {
    const refs=(el.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    // Phenom points aria-labelledby at a changing picker status/live region;
    // its stable question is the actual associated label, not that status.
    const labelled=refs.map(id=>el.getRootNode().getElementById(id))
      .filter(n=>n && !(el.closest('.react-datepicker-wrapper') && el.labels?.length && n.hasAttribute('aria-live')))
      .map(n=>n.textContent||'').join(' ').trim();
    if(labelled) return labelled.replace(/\s+/g,' ');
    // Bare Rippling controls say 'Select'; retain their surrounding question.
    if(el.matches('input,select,textarea') && el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    let label=[...(el.labels||[])].map(n=>n.innerText||n.textContent||'').join(' ').trim();
    if(!label && el.id) {const l=el.getRootNode().querySelector('label[for="'+CSS.escape(el.id)+'"]');label=l?.innerText||l?.textContent||'';}
    if(!label && el.getAttribute('role')==='combobox') {
      for(let p=el.parentElement,h=0;p&&h<4;p=p.parentElement,h++) {
        if(p.querySelectorAll('input,select,textarea,[role=combobox]').length>1) break;
        const l=p.querySelector('label,[class*=custom-question-label]');
        if(l) {label=l.innerText||l.textContent||'';break;}
      }
    }
    return (label||el.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim();
  };
  // Legend of the fieldset that contains this checkbox/radio and no other one.
  const soleLegend = el => {
    const fs = el.closest('fieldset');
    const lg = fs && fs.querySelector(':scope > legend');
    if (!lg || fs.querySelectorAll('input[type=checkbox], input[type=radio]').length !== 1) return '';
    return (lg.innerText || lg.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const controls = [];
  const seen = new Set();
  const addCtl = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') return;
    // Avature repeatable sections (education, work history) ship a hidden
    // TEMPLATE row (`data-samplerow`, ids ending "-sample") next to the real
    // one. Listing it gave Wingtip's form 61 controls: the agent filled the
    // invisible template's School/dates first, and the real privacy-consent
    // checkbox fell past the 60-control cap so it could never be ticked.
    const sampleRow = el.closest('[data-samplerow]');
    if (sampleRow && !sampleRow.getClientRects().length) return;
    // select2 renders a role=combobox <span> beside its backing <select>.
    // When the select itself is listed, the span is the same field twice.
    const s2c = el.closest && el.closest('.select2-container');
    if (s2c && !el.matches('input, select, textarea')) {
      const backing = s2c.previousElementSibling;
      if (backing && backing.tagName === 'SELECT' && seen.has(backing)) return;
    }
    let label = accessibleLabel(el);
    if (!label && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) label = l.innerText || l.textContent || '';
    }
    if (!label) {
      const wrap = el.closest('label');
      if (wrap) label = wrap.innerText || wrap.textContent || '';
    }
    // Avature consent boxes (Wingtip "I have read and agree to the terms of
    // Wingtip's Privacy Policy", Southgate "I Acknowledge") carry an EMPTY
    // <label for>; the question is the <legend> of a fieldset that holds only
    // this one box. Without this the box listed as "6388 | 6388" and Wingtip job
    // 1318 spent 33 steps hunting for the consent it could not recognise.
    if (!label && ['checkbox', 'radio'].includes(type)) label = soleLegend(el);
    if (!label && el.getAttribute('role') === 'combobox') {
      for (let p=el.parentElement,h=0;p&&h<4;p=p.parentElement,h++) {
        if (p.querySelectorAll('input,select,textarea,[role=combobox]').length>1) break;
        const l=p.querySelector('label, [class*=custom-question-label]');
        if(l) {label=l.innerText||l.textContent||'';break;}
      }
    }
    if (!label) {
      let n = el.previousElementSibling, hops = 0;
      while (n && hops++ < 3 && !label) {
        const t = (n.innerText || n.textContent || '').trim();
        if (t && t.length < 120) label = t;
        n = n.previousElementSibling;
      }
      if (!label && el.parentElement) {
        const t = (el.parentElement.innerText || '').trim();
        if (t && t.length < 120) label = t;
        // A consent checkbox often has no <label>, just a long sentence beside
        // it (Bellows/Zwayam). Show its opening words rather than a test id,
        // so the agent can recognise it and set it by number.
        else if (t && ['checkbox', 'radio'].includes(type))
          label = t.replace(/\s+/g, ' ').slice(0, 90) + '...';
      }
    }
    label = (label || '').replace(/\s+/g, ' ').trim();
    // A dropdown's accessible name often ends with its CURRENT value (Angular
    // mat-select: "UG Qualification B.Tech/B.E."; Workday button aria-label:
    // "Phone Device Type Mobile"). Drop it so the label, and the signature
    // built from it, stay the same before and after a pick.
    if (el.matches('[role=combobox]:not(input):not(select):not(textarea), button[aria-haspopup=listbox]')) {
      const shown = (el.querySelector('.mat-mdc-select-value-text, .mat-select-value-text')?.innerText ||
                     (el.tagName === 'BUTTON' ? el.innerText : '') || '').replace(/\s+/g, ' ').trim();
      if (shown && label.length > shown.length && label.endsWith(shown))
        label = label.slice(0, -shown.length).trim();
    }
    const meta = [label, el.name, el.id, el.getAttribute('aria-label'),
                  el.getAttribute('placeholder'), el.getAttribute('data-testid')]
                 .filter(Boolean).join(' | ');
    // A signature that survives a re-enumeration but changes if the control at
    // this position is genuinely a different one.
    const sig = [el.tagName, el.id || '', el.name || '', norm(label).slice(0, 40),
      type, ['radio','checkbox'].includes(type) ? el.value : ''].join('~');
    controls.push({el: el, label: label, meta: meta, metaN: norm(meta), sig: sig});
  };
  // Bare comboboxes are set through their own real listbox, not a value setter.
  // Wrappers with backing controls still represent only one logical field.
  const SETTABLE = 'select, input, textarea';
  // Workday: a <button aria-haspopup=listbox> IS the control; the 0x0 text
  // input beside it only stores the chosen option's id. List the button, never
  // the store (writing the store is ignored by Workday).
  const LISTBOX_BTN = 'button[aria-haspopup=listbox]';
  document.querySelectorAll(LISTBOX_BTN).forEach(b => {
    (b.parentElement ? [...b.parentElement.querySelectorAll('input')] : []).forEach(i => {
      const r = i.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) seen.add(i);
    });
  });
  document.querySelectorAll(SETTABLE + ', ' + LISTBOX_BTN).forEach(addCtl);
  document.querySelectorAll('[role=combobox], [role=listbox]').forEach(w => {
    if (w.matches(SETTABLE)) { addCtl(w); return; }
    const inner = w.querySelector(SETTABLE);
    if (inner) addCtl(inner);
    else if (w.getAttribute('role') === 'combobox' &&
             !w.querySelector('[role=combobox]')) addCtl(w);
  });
  document.querySelectorAll('*').forEach(h => {
    if (h.shadowRoot) {
      h.shadowRoot.querySelectorAll(SETTABLE).forEach(addCtl);
      h.shadowRoot.querySelectorAll('[role=combobox]').forEach(w => {
        if (w.matches(SETTABLE)) { addCtl(w); return; }
        const inner = w.querySelector(SETTABLE);
        if (inner) addCtl(inner);
        else if (w.getAttribute('role') === 'combobox' &&
                 !w.querySelector('[role=combobox]')) addCtl(w);
      });
    }
  });
"""

# Enumerate the page's controls so the agent can address one by NUMBER.
_LIST_FIELDS_JS = r"""
(function() {
__COLLECT__
  const describe = (c) => {
    const el = c.el;
    let kind = el.tagName.toLowerCase();
    if (el.tagName === 'SELECT') {
      const real = Array.from(el.options || []).filter(o => (o.value || '').trim() !== '');
      kind = real.length ? 'select' : 'select(remote/empty)';
    } else if (el.getAttribute('role') === 'combobox' ||
               el.getAttribute('aria-autocomplete') ||
               (el.className || '').toString().toLowerCase().includes('select2') ||
               (el.className || '').toString().toLowerCase().includes('chosen')) {
      kind = 'combobox';
    } else {
      kind = (el.getAttribute('type') || 'text').toLowerCase();
    }
    let opts = null;
    if (el.tagName === 'SELECT') {
      // EVERY option, not a sample. Truncating this list is how job570 spent
      // turns guessing "Bachelor's Degree" at a field whose real option was
      // "Bachelor - Engineering" - the right answer was hidden behind "...".
      opts = Array.from(el.options || [])
                  .filter(o => (o.value || '').trim() !== '')
                  .map(o => (o.text || '').trim());
    }
    let cur = el.matches('[role=combobox]:not(input):not(select)') ?
      (el.innerText || el.textContent || '').trim().slice(0,40) : '';
    try { if ('value' in el) cur = (el.value || '').toString().slice(0, 40); } catch (e) {}
    if (el.matches('input[data-uxi-widget-type=selectinput]')) {
      // Workday search prompt: what counts is the picked pills, not the
      // search text typed into the box.
      kind = 'combobox';
      const box = el.closest('[data-automation-id=multiselectInputContainer]') || el.parentElement;
      cur = [...box.querySelectorAll('[data-automation-id=selectedItem]')]
              .map(n => n.getAttribute('title') || n.innerText.trim()).join(', ').slice(0, 60);
    }
    if (el.matches('button[aria-haspopup=listbox]')) {
      kind = 'combobox';
      const t = (el.innerText || '').trim();
      cur = /^select one$/i.test(t) ? '' : t.slice(0, 40);
    }
    if (el.matches('input') && el.closest('.react-datepicker-wrapper')) kind='calendar-picker';
    // A committed react-select shows its value in a singleValue node, NOT in
    // the input (which goes blank after selection). Surfacing it is what lets
    // the agent tell two same-named controls apart: on Proseware's Greenhouse
    // form (job591, 2026-09-11) the phone-prefix widget and the real Country
    // field BOTH listed as "Country", the agent addressed the prefix, and
    // spent its whole budget trying to put "India" into a box that only holds
    // dial codes. Showing `currently='+91'` makes the wrong one obvious.
    if (!cur) {
      let ctl = null;
      for (let n = el.parentElement, h = 0; n && h < 6; n = n.parentElement, h++) {
        const c = (n.className || '').toString();
        if (/select__control|react-select__control|select2-container|chosen-container/.test(c)) {
          ctl = n; break;
        }
      }
      if (ctl) {
        const sv = ctl.querySelector(
          '[class*="singleValue"], [class*="multiValue"], .select2-selection__rendered');
        if (sv) cur = (sv.innerText || sv.textContent || '').trim().slice(0, 40);
      }
    }
    return {label: (c.label || c.meta || '(unlabelled)').slice(0, 80),
            meta: c.meta.slice(0, 120), kind: kind, sig: c.sig,
            options: opts, value: cur,
            checked: ['radio','checkbox'].includes(el.type) ? !!el.checked : null,
            required: !!(el.required || el.getAttribute('aria-required') === 'true' ||
              el.closest('[aria-required=true]') || /\*\s*$/.test(c.label))};
  };
  return JSON.stringify({fields: controls.slice(0, 60).map(describe), total: controls.length});
})()
""".replace("__COLLECT__", _COLLECT_JS)

# The JS below runs in the page. It is deliberately one self-contained
# function so the whole operation is a single CDP round-trip: find the field,
# set it the right way for its widget type, notify the framework, read it
# back. Returning a JSON verdict keeps the Python side simple.
_SET_FIELD_JS = r"""
(async function(fieldHint, wantValue, strict, byIndex, expectSig) {
  const out = {ok: false, strategy: null, matched: null, readback: null, note: null,
               candidates: [], sig: null, stale: false, reverted: false,
               noMatch: false, emptyRemote: false, availableOptions: null};

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // THE most important thing in this file: a value that reads back correctly
  // one millisecond after being set may be WIPED by the framework's next
  // render. Wingtip's Avature "School" and Bluegum's Greenhouse "School"/"Degree"
  // both did exactly that - our tool reported success 15+ times while the
  // form stayed empty and Submit kept failing "this field is required".
  // So every success path settles first and re-reads, and reports honestly
  // if the value did not survive.
  const SETTLE_MS = 600;
  const finish = async (readFn, expectedN) => {
    await sleep(SETTLE_MS);
    let after = '';
    try { after = (readFn() || '').toString(); } catch (e) {}
    out.readback = after;
    const afterN = norm(after);
    out.ok = afterN !== '' && (afterN === expectedN ||
                               afterN.includes(expectedN) ||
                               expectedN.includes(afterN));
    if (!out.ok) {
      out.reverted = true;
      out.note = 'the value did not survive the form re-rendering (field now ' +
                 JSON.stringify(after) + '). This widget discards values set ' +
                 'programmatically.';
    }
    return JSON.stringify(out);
  };
__COLLECT__
  const hint = norm(fieldHint);
  const want = (wantValue || '').toString();
  const wantN = norm(want);

  out.candidates = controls.slice(0, 40).map(c => c.meta).filter(Boolean);

  let target = null;

  // ---- addressing by NUMBER: no interpretation, no fuzzy matching ----
  if (byIndex !== null && byIndex !== undefined && byIndex >= 0) {
    target = controls[byIndex] || null;
    // The DOM may have been rewritten between listing and setting (multi-step
    // forms do this constantly). Refuse rather than fill a different field.
    // But a control that merely MOVED is still the listed one: on Avature,
    // picking Country/State or a college of "Other" inserts rows, so every
    // later number in the agent's batch shifted (Kestrel 1622/1628 burned
    // the 25-set cap on that). The signature carries tag, id, name, label and
    // type, so exactly one control with it is the field the agent was shown.
    if (expectSig && (!target || target.sig !== expectSig)) {
      const same = controls.filter(c => c.sig === expectSig);
      if (same.length === 1) {
        target = same[0];
        out.movedTo = controls.indexOf(target) + 1;
      }
    }
    if (!target) {
      out.stale = !!expectSig;
      out.note = (expectSig ? 'the form changed since the fields were listed: ' : '') +
                 'field #' + (byIndex + 1) + ' does not exist; the page lists ' +
                 controls.length + ' controls. Re-list the fields.';
      return JSON.stringify(out);
    }
    if (expectSig && target.sig !== expectSig) {
      out.stale = true;
      out.note = 'the form changed since the fields were listed (#' +
                 (byIndex + 1) + ' is now ' + (target.meta || target.sig) +
                 '). Re-list the fields and use the new number.';
      return JSON.stringify(out);
    }
  }

  // ---- choose the control matching the hint ----
  // Only when no number was given. Score every control instead of taking the
  // first loose hit. An early version returned the FIRST control whose
  // metadata contained any hint token, so "College Name (the select2 combobox
  // ... NOT the Full name field)" matched the *Full name* input twice in a row
  // on a live test, because the agent's descriptive hint happened to contain
  // the word "name". Ranking by how much of the CONTROL is explained by the
  // hint fixes that: a 4-character "name" overlapping a long hint scores far
  // below a control whose own label is genuinely "College Name".
  if (!target && hint) {
    target = controls.find(c => c.metaN === hint);
    if (!target) {
      const stop = new Set(['the','not','field','with','and','for','ref','which','that','this',
                            'select','input','combobox','labeled','underlying','hidden','likely',
                            'named','box','dropdown','value','currently','showing','placeholder',
                            'json','key','visible','label','required','per','debug','despite',
                            'text','search','form','bound','empty']);
      const htAll = hint.split(/[^a-z0-9]+/).filter(t => t.length > 2 && !stop.has(t));
      // Tokens the hint contributes that are NOT generic form words. These
      // carry the actual identity of the field ("college" in "College Name").
      const generic = new Set(['name','date','number','code','address','phone','email','title',
                               'period','type','level','years','other','details','information']);
      const htKey = htAll.filter(t => !generic.has(t));
      let best = null, bestScore = 0, runnerUp = 0, runnerEl = null;
      for (const c of controls) {
        const ct = c.metaN.split(/[^a-z0-9]+/).filter(t => t.length > 2 && !stop.has(t));
        if (!ct.length) continue;
        const shared = ct.filter(t => htAll.includes(t));
        if (!shared.length) continue;
        // Coverage of the CONTROL by the hint...
        let score = shared.length / ct.length;
        // ...but a match on a DISTINCTIVE hint token matters far more than a
        // match on a generic one. Without this, hint "College Name" scored
        // "Full name" (shares the generic "name") above the actual college
        // control, and filled the wrong field three times on a live run.
        const keyHits = ct.filter(t => htKey.includes(t)).length;
        if (htKey.length) {
          if (keyHits) score += 2.0 * (keyHits / htKey.length);
          else score -= 0.75;  // matched only generic words: probably wrong
        }
        if (hint.includes(c.metaN) && c.metaN.length > 3) score += 1.0;
        if (c.metaN.includes(hint) && hint.length > 3) score += 1.0;
        if (shared.length === ct.length) score += 0.5;
        if (score > bestScore) {
          runnerUp = bestScore; runnerEl = best ? best.el : null;
          bestScore = score; best = c;
        }
        else if (score > runnerUp) { runnerUp = score; runnerEl = c.el; }
      }
      // A select2 widget and the hidden <select> it drives are BOTH collected,
      // and a hint like "College" legitimately matches both. That is not a
      // real ambiguity - either one ends up setting the same form value - so
      // resolve it to the widget rather than making the agent disambiguate
      // something that cannot be got wrong.
      const backingOf = (el) => {
        if (!el) return null;
        if (el.tagName === 'SELECT') return el;
        const sc = el.closest('.select2-container, .select2, .chosen-container, ' +
                              '[class*=select2], [class*=chosen]') || el.parentElement;
        let h = (sc && sc.querySelector) ? sc.querySelector('select') : null;
        if (!h && el.id) {
          const base = el.id.replace(/[-_](input|search|display|text)$/i, '');
          const cand = document.getElementById(base);
          if (cand && cand.tagName === 'SELECT') h = cand;
        }
        return h || el;
      };
      const samePair = best && runnerEl &&
                       backingOf(best.el) && backingOf(best.el) === backingOf(runnerEl);
      // A prose hint must be CONFIDENTLY resolved or refused. Guessing is how
      // "Springfield Institute of Technology" ended up in the Full name box while reporting success.
      // Two independent bars: an absolute floor, and a clear margin over the
      // runner-up so an ambiguous page forces the agent to use a number.
      if (bestScore > 0 && best) {
        const floor = strict ? 1.2 : 0.9;
        if (bestScore < floor ||
            (!samePair && runnerUp > 0 && bestScore - runnerUp < 0.35)) {
          out.note = 'that description does not identify one field confidently ' +
                     '(best guess "' + best.meta.slice(0, 60) + '"). Use the field ' +
                     'NUMBER instead: print NEED_FIELDS to list them.';
          out.ambiguous = true;
          return JSON.stringify(out);
        }
        target = best;
      }
    }
  }
  if (!target) { out.note = 'no form control matched the field hint'; return JSON.stringify(out); }

  out.matched = target.meta;
  out.sig = target.sig;
  const el = target.el;
  const specialKind = ['radio','checkbox'].includes(el.type) ? el.type :
    el.matches('[role=combobox]:not(input):not(select):not(textarea), button[aria-haspopup=listbox]') ? 'bare-combobox' :
    el.matches('input[data-uxi-widget-type=selectinput]') ? 'workday-prompt' :
    el.matches('input') && el.closest('.react-datepicker-wrapper') ? 'calendar' : '';
  if (specialKind) {
    // Preserve the exact indexed/signature-checked node for trusted CDP UI input.
    const token='__jobo_widget_'+crypto.randomUUID();
    const identity=()=>[el.tagName,el.type,el.id,el.name,el.form,el.getRootNode(),
      // A dropdown's accessible name embeds its CURRENT value (Workday button
      // aria-label "Phone Device Type Mobile"; Angular mat-select
      // aria-labelledby gains the value-text id). Picking an option changes
      // it, so for dropdowns it is not identity; id/name/root/form are.
      specialKind==='bare-combobox' ? null : accessibleLabel(el),
      specialKind==='bare-combobox' ? null : el.getAttribute('aria-label'),
      specialKind==='bare-combobox' ? null : el.getAttribute('aria-labelledby'),
      ['radio','checkbox'].includes(specialKind) ? el.value : null];
    const original=identity(), originalValue=el.value;
    window[token]={el, want, out, kind:specialKind, label:target.label, originalValue,
      valid:()=>el.isConnected && identity().every((v,i)=>v===original[i])};
    return JSON.stringify({...out, widgetToken:token});
  }

  // React/Vue intercept value assignment; use the native setter so the
  // framework's own onChange actually observes the new value.
  const nativeSet = (node, value) => {
    const proto = node instanceof HTMLSelectElement ? HTMLSelectElement.prototype
                : node instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(node, value); else node.value = value;
  };
  const fire = (node) => {
    node.dispatchEvent(new Event('input', {bubbles: true}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
    // select2/chosen listen on jQuery's event bus, not the native one.
    try {
      const jq = window.jQuery || window.$;
      if (jq && jq(node).length) { jq(node).trigger('change'); jq(node).trigger('change.select2'); }
    } catch (e) {}
  };

  // ---- select2 with a REMOTE (ajax) catalogue: drive its own search ----
  // Avature (Wingtip "School") renders select2 over an EMPTY <select> and loads
  // options from a JSON search endpoint after a debounce (Wingtip: 1000 ms).
  // The old path saw zero options and reported "no results" in ~9 ms without
  // ever sending a search. The select2 instance also lives on a private
  // jQuery copy (Avature: window.avajQuery / pjQuery, 3.7.1) rather than
  // window.jQuery (3.4.1, no select2), so jQuery(el).data('select2') is empty
  // and every jQuery trigger we fired went to the wrong library.
  const s2jq = (node) => {
    const cands = [];
    try { if (window.jQuery) cands.push(window.jQuery); } catch (e) {}
    try {
      for (const k of Object.keys(window)) {
        let v = null;
        try { v = window[k]; } catch (e) { continue; }
        if (v && typeof v === 'function' && v.fn && v.fn.select2 && !cands.includes(v)) cands.push(v);
      }
    } catch (e) {}
    for (const jq of cands) {
      try { if (jq.fn && jq.fn.select2 && jq(node).data('select2')) return jq; } catch (e) {}
    }
    return null;
  };
  const s2isRemote = (jq, node) => {
    try { return !!jq(node).data('select2').options.options.ajax; } catch (e) { return false; }
  };
  const s2Drive = async (jq, sel, wantText) => {
    const inst = jq(sel).data('select2');
    out.remoteSearch = true;
    // Punctuation-blind comparison: "(School not listed)" == "School not listed".
    const pn = s => norm(s).replace(/[^a-z0-9]+/g, ' ').trim();
    const flatten = rs => rs.flatMap(r => (r && r.children) ? flatten(r.children) : [r]);
    if (!inst.__joboResults) {
      inst.__joboResults = {};
      // The widget's own "results arrived for term X" event: unambiguous,
      // unlike watching the DOM, which still shows the previous term's list.
      inst.on('results:all', p => {
        inst.__joboResults[(p && p.query && p.query.term) || ''] =
          flatten((p && p.data && p.data.results) || []);
      });
    }
    const searchField = () =>
      (inst.dropdown && inst.dropdown.$search && inst.dropdown.$search[0]) ||
      (inst.selection && inst.selection.$search && inst.selection.$search[0]) ||
      document.querySelector('.select2-container--open .select2-search__field');
    const close = () => {
      try { const f = searchField(); if (f) nativeSet(f, ''); } catch (e) {}
      try { if (inst.isOpen()) jq(sel).select2('close'); } catch (e) {}
    };
    const search = async (term) => {
      delete inst.__joboResults[term];
      if (!inst.isOpen()) { jq(sel).select2('open'); await sleep(200); }
      const f = searchField();
      if (!f) return null;
      f.focus();
      nativeSet(f, term);
      f.dispatchEvent(new InputEvent('input', {bubbles: true, data: term, inputType: 'insertText'}));
      const delay = ((inst.options.options.ajax || {}).delay) || 250;
      const end = Date.now() + delay + 3500;
      while (Date.now() < end && !(term in inst.__joboResults)) await sleep(120);
      return (term in inst.__joboResults) ? inst.__joboResults[term] : null;
    };
    const real = rs => (rs || []).filter(r => r && r.id !== undefined && r.id !== null &&
                                              String(r.id) !== '' && !r.disabled && !r.loading);
    const res = await search(wantText);
    if (res === null) {
      close();
      out.note = 'the search widget did not answer for ' + JSON.stringify(wantText) +
                 ' (no response from its catalogue). Try once more.';
      return JSON.stringify(out);
    }
    const found = real(res);
    // EXACT (punctuation-blind) only. A loose rule would answer "Indian
    // Institute of Information Technology, Allahabad" for "Indian Institute
    // of Information Technology" - a different school on a real application.
    const hit = found.find(r => pn(r.text) === pn(wantText));
    if (hit) {
      const li = [...document.querySelectorAll(
        '.select2-container--open .select2-results__option')].find(
        o => pn(o.innerText || o.textContent || '') === pn(hit.text));
      if (li) {
        // select2 commits on mouseup (delegated jQuery handler).
        li.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
        await sleep(150);
      }
      if (String(sel.value) !== String(hit.id)) {
        try { inst.trigger('select', {data: hit}); } catch (e) {}
        await sleep(150);
      }
      close();
      out.strategy = 'select2-remote-search';
      return await finish(() => (sel.options[sel.selectedIndex] || {}).text || '', norm(hit.text));
    }
    // Nothing exact. The catalogue may still hold an explicit escape hatch
    // ("(School not listed)", "Other"); look it up so the honest choice is
    // visible, but never pick a merely similar real entry.
    const ESC = /^((school|college|university|institution|institute|company|employer|degree|major)\s+)?(not listed|other|other please specify|none of the above)$/;
    // A shared catalogue can hold escapes for OTHER fields: Wingtip's School
    // search for "not listed" also returns "Company Not Listed". Accept a
    // prefixed escape only when its noun belongs to this field's own label.
    const NOUNS = [['school','college','university','institution','institute'],
                   ['company','employer'], ['degree'], ['major']];
    const labelN = pn(target.label || target.meta || '');
    const fitsField = (txt) => {
      const first = pn(txt).split(' ')[0];
      const grp = NOUNS.find(g => g.includes(first));
      return !grp || grp.some(n => labelN.split(' ').includes(n));
    };
    const escapes = [];
    for (const term of ['not listed', 'other']) {
      const r = real(await search(term) || []).filter(x => ESC.test(pn(x.text)) && fitsField(x.text));
      r.forEach(x => { if (!escapes.includes(x.text)) escapes.push(x.text); });
      if (escapes.length) break;
    }
    close();
    out.noMatch = true;
    out.emptyRemote = found.length === 0;
    out.availableOptions = [...found.slice(0, 20).map(r => r.text), ...escapes]
      .filter((v, i, a) => v && a.indexOf(v) === i);
    out.note = 'searching this field\'s catalogue for ' + JSON.stringify(wantText) +
      (found.length ? ' found only: ' + found.slice(0, 20).map(r => r.text).join(' / ')
                    : ' returned no results') +
      (escapes.length ? '. It offers the escape option(s): ' + escapes.join(' / ')
                      : '. It has no "not listed"/"Other" option');
    return JSON.stringify(out);
  };

  // ---- native <select>: match an option by text or value ----
  if (el.tagName === 'SELECT') {
    const opts = Array.from(el.options || []);
    let pick = opts.find(o => norm(o.text) === wantN || norm(o.value) === wantN);
    if (!pick) pick = opts.find(o => norm(o.text).includes(wantN) && wantN.length > 1);
    if (!pick) pick = opts.find(o => wantN.includes(norm(o.text)) && norm(o.text).length > 1);
    const jqS2 = pick ? null : s2jq(el);
    if (jqS2 && s2isRemote(jqS2, el)) return await s2Drive(jqS2, el, want);
    if (pick) {
      el.value = pick.value;
      el.selectedIndex = pick.index;
      fire(el);
      out.strategy = el.multiple ? 'native-select-multi' : 'native-select';
      return await finish(
        () => (el.options[el.selectedIndex] || {}).text || el.value, norm(pick.text));
    }
    // No option matched.
    //
    // We used to INVENT an option here when the select was empty apart from a
    // placeholder, on the theory that a remote-search widget would have added
    // the same option after its server round-trip. That was a lie, and an
    // expensive one: on 2026-09-06 Wingtip's "School" was reported SET eight
    // times running while the form held a school that does not exist in Wingtip's
    // database, and the agent's own live search confirmed "No results found"
    // for it. The form rejected the application, our log claimed success, and
    // the run burned its budget re-setting a field that was never real.
    //
    // An option list is the SITE's vocabulary, not ours. If nothing in it is
    // true for the candidate, that is a genuine blocker and the agent must hear it.
    const realOpts = opts.filter(o => (o.value || '').trim() !== '');
    out.noMatch = true;
    out.availableOptions = realOpts.map(o => (o.text || '').trim()).filter(Boolean);
    if (realOpts.length === 0) {
      out.note = 'this is a search-as-you-type field with nothing preloaded, ' +
                 'and typing ' + JSON.stringify(want) + ' returned no results. ' +
                 'The value does not exist in this site\'s list.';
      out.emptyRemote = true;
    } else {
      out.note = 'no option matched ' + JSON.stringify(want) + '; the site offers: ' +
                 out.availableOptions.join(' / ');
    }
    return JSON.stringify(out);
  }

  // ---- date inputs: value must be in the format the ELEMENT expects ----
  // A native <input type="date"> only accepts an ISO yyyy-mm-dd value; assign
  // "10/04/2026" and it silently stays empty, which is exactly how job489's
  // "Earliest start date" looked filled while the form held nothing. Composite
  // month/day/year spinbutton widgets have the same shape: the visible
  // segments accept typed digits but the backing input never updates.
  const parseDate = (s) => {
    const t = (s || '').toString().trim();
    let m;
    // yyyy-mm-dd (already ISO)
    if ((m = t.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/)))
      return {y: +m[1], mo: +m[2], d: +m[3]};
    // dd/mm/yyyy or mm/dd/yyyy - ambiguous, so use the field's own locale
    // hint when there is one, else assume the US order these ATS forms use.
    if ((m = t.match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})$/))) {
      const a = +m[1], b = +m[2];
      const ph = ((el.getAttribute('placeholder') || '') + ' ' +
                  (el.getAttribute('aria-label') || '')).toLowerCase();
      const dayFirst = ph.includes('dd/mm') || ph.includes('dd-mm');
      if (dayFirst) return {y: +m[3], mo: b, d: a};
      // Unambiguous when one value cannot be a month.
      if (a > 12) return {y: +m[3], mo: b, d: a};
      return {y: +m[3], mo: a, d: b};
    }
    // "4 October 2026" / "October 4, 2026"
    const MONTHS = ['january','february','march','april','may','june','july',
                    'august','september','october','november','december'];
    const lower = t.toLowerCase();
    const mi = MONTHS.findIndex(name => lower.includes(name.slice(0, 3)));
    if (mi >= 0) {
      const yr = (lower.match(/\b(\d{4})\b/) || [])[1];
      const dy = (lower.match(/\b(\d{1,2})\b(?!\d)/) || [])[1];
      if (yr && dy) return {y: +yr, mo: mi + 1, d: +dy};
    }
    return null;
  };
  const pad = n => (n < 10 ? '0' + n : '' + n);

  const inputType = (el.getAttribute('type') || '').toLowerCase();

  // ---- native <input type="month">: the value MUST be "yyyy-mm" ----
  // Avature (Wingtip "Start Date" / "Graduation Date") uses these. The browser
  // sanitises any other string to "" on assignment, so the agent's natural
  // "11/2022" read back empty and was misreported as "did not survive the
  // form re-rendering". Normalise the common month spellings first.
  if (el.tagName === 'INPUT' && inputType === 'month') {
    const t = want.trim();
    const MON = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
    let y = null, mo = null, m;
    if ((m = t.match(/^(\d{4})[-\/.](\d{1,2})(?:[-\/.]\d{1,2})?$/))) { y = +m[1]; mo = +m[2]; }
    else if ((m = t.match(/^(\d{1,2})[-\/.](\d{4})$/))) { mo = +m[1]; y = +m[2]; }
    else if ((m = t.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{4})$/))) {
      // A full date: the month is the field that can be one (US order when ambiguous).
      const a = +m[1], b = +m[2]; y = +m[3]; mo = a > 12 ? b : a;
    } else {
      const lower = t.toLowerCase();
      const mi = MON.findIndex(n => new RegExp('\\b' + n).test(lower));
      const yr = (lower.match(/\b(\d{4})\b/) || [])[1];
      if (mi >= 0 && yr) { y = +yr; mo = mi + 1; }
    }
    if (!y || !mo || mo < 1 || mo > 12) {
      out.note = 'this is a month picker; give the value as yyyy-mm (e.g. 2022-11), ' +
                 'could not read ' + JSON.stringify(want);
      return JSON.stringify(out);
    }
    const ym = y + '-' + pad(mo);
    el.focus();
    nativeSet(el, ym);
    fire(el);
    el.blur();
    out.strategy = 'native-month';
    return await finish(() => el.value, ym);
  }

  const looksDateish = inputType === 'date' ||
    /date|dob|birth|joining|start|available/i.test(
      (target.meta || '') + ' ' + (el.getAttribute('placeholder') || ''));

  if (el.tagName === 'INPUT' && (inputType === 'date' || (looksDateish && parseDate(want)))) {
    const d = parseDate(want);
    if (!d) {
      out.note = 'could not understand ' + JSON.stringify(want) +
                 ' as a date; use yyyy-mm-dd';
      return JSON.stringify(out);
    }
    const iso = d.y + '-' + pad(d.mo) + '-' + pad(d.d);
    // A real date input wants ISO; a text field mimicking one wants whatever
    // its placeholder implies, so try ISO first and fall back to the literal.
    const attempts = inputType === 'date' ? [iso] : [want, iso];
    for (const v of attempts) {
      nativeSet(el, v);
      fire(el);
      if ((el.value || '') !== '') {
        out.strategy = inputType === 'date' ? 'native-date' : 'date-text-input';
        await sleep(SETTLE_MS);
        out.readback = el.value;
        // Verify the FORM holds a real date AFTER the framework has settled,
        // not just that we typed something.
        const parsedBack = parseDate(el.value);
        out.ok = !!parsedBack && parsedBack.y === d.y &&
                 parsedBack.mo === d.mo && parsedBack.d === d.d;
        if (out.ok) return JSON.stringify(out);
        out.reverted = true;
      }
    }
    out.note = 'date did not stick (readback=' + el.value + '); the widget may ' +
               'need its segments set individually';
    out.readback = el.value;
    return JSON.stringify(out);
  }

  // ---- plain text input that is NOT a combobox: set it directly ----
  // Do this BEFORE looking for a backing select. A generic ancestor walk can
  // reach an unrelated <select> elsewhere in the form (observed: "Notice
  // Period" text box resolving to the Country dropdown), which would report a
  // confusing failure and leave the real field empty.
  const isCombo = el.getAttribute('role') === 'combobox' ||
                  el.getAttribute('aria-autocomplete') ||
                  (el.className || '').toString().toLowerCase().includes('select2') ||
                  (el.className || '').toString().toLowerCase().includes('chosen');
  if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && !isCombo) {
    // Empty success is intentionally local to real plaintext controls. Never
    // relax finish(): an empty dropdown/read failure is not a committed choice.
    const clearing = want === '';
    const isPlainText = el.tagName === 'TEXTAREA' ||
      ['text','search','email','url','tel','password'].includes(el.type);
    if (clearing && (!isPlainText || el.disabled || el.readOnly)) {
      out.note = 'empty clearing requires an editable plaintext control';
      return JSON.stringify(out);
    }
    const identity = () => [el.type, el.id, el.name, el.form, el.getRootNode(),
      accessibleLabel(el), el.getAttribute('role'), el.getAttribute('aria-autocomplete')];
    const original = clearing ? identity() : null;
    nativeSet(el, want);
    fire(el);
    out.strategy = 'text-input';
    if (clearing) {
      await sleep(SETTLE_MS);
      // Retain the exact node: do not re-query an identically labelled
      // replacement, and do not interpret a missing/throwing read as empty.
      out.readback = el.value;
      out.ok = el.isConnected && !el.disabled && !el.readOnly &&
        identity().every((v, i) => v === original[i]) && el.value === '';
      if (!out.ok) {
        out.reverted = true;
        out.note = 'clear did not survive on the same connected plaintext field; re-list the fields';
      }
      return JSON.stringify(out);
    }
    return await finish(() => el.value, wantN);
  }

  // ---- react-select / headless comboboxes: DRIVE the widget, don't inject ----
  // Greenhouse, Tamarack, Proseware and Wingtip all use widgets that keep their
  // value in framework state, not in the DOM. Setting the input's value is
  // discarded on the next render (this is the "resets to empty the instant you
  // click Submit" failure). The only reliable route is to do what a human
  // does: focus, type, wait for the listbox, and click a real option with real
  // pointer events so the widget's own handlers commit the selection.
  const realClick = (node) => {
    const r = node.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const opts = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y};
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown',
                        'pointerup', 'mouseup', 'click']) {
      const Ctor = type.startsWith('pointer') ? (window.PointerEvent || MouseEvent)
                                              : MouseEvent;
      node.dispatchEvent(new Ctor(type, opts));
    }
  };

  const typeInto = (node, text) => {
    node.focus();
    nativeSet(node, '');
    node.dispatchEvent(new Event('input', {bubbles: true}));
    nativeSet(node, text);
    node.dispatchEvent(new InputEvent('input', {bubbles: true, data: text}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
    node.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'a'}));
    node.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'a'}));
  };

  // Find the listbox options the widget rendered, wherever it put them
  // (react-select portals them to <body>, outside the field's own container).
  const findOptions = () => {
    const sels = ['[role=option]', '.select__option', '[class*="-option"]',
                  '.select2-results__option', '.chosen-results li'];
    const seenO = new Set(), found = [];
    for (const s of sels) {
      document.querySelectorAll(s).forEach(o => {
        if (seenO.has(o)) return;
        const r = o.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return;   // not actually shown
        seenO.add(o);
        found.push(o);
      });
    }
    return found;
  };

  // Locate a backing hidden <select> now, because it decides WHICH strategy
  // owns this widget. A select2-style control with a real backing select is
  // best served by setting that select (fast, no listbox timing); only a
  // widget with NO backing element needs the slow drive-the-UI route.
  const findBacking = () => {
    const sc = el.closest('.select2-container, .select2, .chosen-container, ' +
                          '[class*=select2], [class*=chosen]') || el.parentElement;
    let h = (sc && sc.querySelector) ? sc.querySelector('select') : null;
    if (!h && el.id) {
      const base = el.id.replace(/[-_](input|search|display|text)$/i, '');
      const cand = document.getElementById(base);
      if (cand && cand.tagName === 'SELECT') h = cand;
    }
    return h;
  };

  const isReactCombo = (el.getAttribute('role') === 'combobox' ||
                       el.getAttribute('aria-autocomplete') === 'list' ||
                       el.getAttribute('aria-expanded') !== null ||
                       /select__|react-select|-input$/.test((el.className || '').toString()) ||
                       !!el.closest('[class*="select__"], [class*="react-select"]')) &&
                      !findBacking();

  if (el.tagName === 'INPUT' && isReactCombo) {
    // Remember what the widget held BEFORE we touch it, so a failed search can
    // be rolled back completely rather than leaving our query behind.
    const priorValue = el.value || '';
    const abandonTyping = () => {
      try {
        nativeSet(el, priorValue);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        // Close the open menu the way a user would, so the widget clears its
        // own pending state instead of holding a half-open search.
        el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Escape'}));
        el.blur();
      } catch (e) {}
    };
    const opener = el.closest('[class*="control"], [class*="select__"], ' +
                              '[role=combobox]') || el.parentElement || el;
    realClick(opener);
    await sleep(120);
    typeInto(el, want);
    // Remote-search widgets need time to fetch; poll rather than guess.
    let opts = [];
    for (let i = 0; i < 14; i++) {
      await sleep(160);
      opts = findOptions();
      if (opts.length) break;
    }
    if (opts.length) {
      const score = (o) => {
        const t = norm(o.innerText || o.textContent || '');
        if (!t) return -1;
        if (t === wantN) return 3;
        if (t.startsWith(wantN) || wantN.startsWith(t)) return 2;
        if (t.includes(wantN) || wantN.includes(t)) return 1;
        return -1;
      };
      let best = null, bestS = 0;
      for (const o of opts) { const s = score(o); if (s > bestS) { bestS = s; best = o; } }
      if (best) {
        best.scrollIntoView({block: 'center'});
        await sleep(60);
        realClick(best);
        out.strategy = 'react-select-driven';
        out.matched = target.meta;
        // Read the widget's DISPLAYED value, which is where these widgets show
        // their committed state (the input itself goes blank after selection).
        return await finish(() => {
          // Walk to the widget's VALUE container, not merely the first
          // ancestor whose class mentions the library.
          //
          // `el.closest` and a naive `/select__/` test both stop at
          // `select__input-container`, which is a SIBLING of the element
          // holding the selected value - so the lookup finds no singleValue
          // and a correctly-committed selection reads back as reverted.
          // Observed live on Bluegum/Greenhouse (2026-09-11): Country, Degree
          // and the JD yes/no question were all genuinely committed
          // (`select__value-container--has-value`, singleValue text present)
          // while we reported "the value did not survive the form
          // re-rendering" for every one of them, and the agent then spent its
          // budget re-fighting fields that were already correct.
          //
          // Prefer the control/value container explicitly, and only fall back
          // to a looser match.
          let box = null;
          for (let n = el.parentElement, hops = 0; n && hops < 6; n = n.parentElement, hops++) {
            const cls = (n.className || '').toString();
            if (/select__control|select__value-container|react-select__control|select2-container|chosen-container/.test(cls)) {
              box = n; break;
            }
          }
          if (!box) {
            for (let n = el.parentElement, hops = 0; n && hops < 6; n = n.parentElement, hops++) {
              const cls = (n.className || '').toString();
              if (/control|select__|react-select/.test(cls)) { box = n; break; }
            }
          }
          if (!box) box = el.parentElement;
          // The container must still CONTAIN this input. A walk that climbs
          // too far lands on a wrapper holding several widgets, and then
          // querySelector returns a NEIGHBOUR's value - live on Bluegum the
          // Country field read back "+91" from the adjacent phone-prefix
          // select, which is a false success waiting to happen.
          if (!box.contains(el)) box = el.parentElement;
          const single = box && box.querySelector(
            '[class*="singleValue"], [class*="multiValue"], .select2-selection__rendered');
          const shown = single ? (single.innerText || single.textContent || '').trim() : '';
          if (shown) return shown;
          if (el.value) return el.value;
          // Last resort: the container's own text, minus the placeholder.
          const boxText = box ? (box.innerText || '').trim() : '';
          const ph = (el.getAttribute('placeholder') || '').trim();
          return ph && boxText === ph ? '' : boxText;
        }, wantN);
      }
      // Leave NO residue. We typed a query into this widget to make it search;
      // if nothing matched, that text is still sitting in the input. Greenhouse
      // (job516, 2026-09-11) treats a dirty combobox input as pending state and
      // WIPES its sibling fields on the next render - the agent reported
      // "School, Degree and Discipline share state, typing in one clears the
      // others", which was our own leftover text, not their bug. Always restore
      // the widget to the state we found it in before reporting failure.
      abandonTyping();
      out.noMatch = true;
      out.availableOptions = opts.map(o => (o.innerText || o.textContent || '').trim())
                                 .filter(Boolean);
      out.note = 'the dropdown opened but no option matched ' + JSON.stringify(want) +
                 '. The site offers: ' + out.availableOptions.join(' / ');
      return JSON.stringify(out);
    }
    // No options appeared at all. For a react-style combobox that means the
    // widget has nothing to commit, and writing into its input is exactly the
    // false success this rewrite exists to kill (the value is reverted on the
    // next render while we report ok). Fail honestly instead of falling
    // through to the generic text-input path.
    abandonTyping();
    out.note = 'the dropdown produced no options for ' + JSON.stringify(want) +
               '. This widget only accepts a value picked from its own list, ' +
               'so there is nothing to select.';
    // Name what this control ALREADY holds. Two controls on the same form can
    // share a label (Greenhouse renders the phone dial-code picker and the
    // country-of-residence field both as "Country"), and the fastest way for
    // the agent to notice it is aimed at the wrong one is to be told the box
    // currently contains "+91".
    try {
      let ctl2 = null;
      for (let n = el.parentElement, h = 0; n && h < 6; n = n.parentElement, h++) {
        const c = (n.className || '').toString();
        if (/select__control|react-select__control|select2-container|chosen-container/.test(c)) {
          ctl2 = n; break;
        }
      }
      const sv2 = ctl2 && ctl2.querySelector(
        '[class*="singleValue"], [class*="multiValue"], .select2-selection__rendered');
      const held = sv2 ? (sv2.innerText || sv2.textContent || '').trim() : '';
      if (held) {
        out.heldValue = held;
        out.note += ' This control already holds ' + JSON.stringify(held) +
                    ' - if that is not the kind of value you meant to set, you ' +
                    'are on the WRONG control (forms often have two fields ' +
                    'with similar labels). Print NEED_FIELDS and pick the other one.';
      }
    } catch (e) {}
    out.noMatch = true;
    out.emptyRemote = true;
    out.availableOptions = [];
    out.reverted = true;
    return JSON.stringify(out);
  }

  // ---- custom widget: find the backing hidden <select> near it ----
  // Scope tightly: only a real select2/chosen container, or the element's
  // immediate parent. Never a bare `div` ancestor.
  const scope = el.closest('.select2-container, .select2, .chosen-container, ' +
                           '[class*=select2], [class*=chosen]') || el.parentElement;
  let hidden = (scope && scope.querySelector) ? scope.querySelector('select') : null;
  if (!hidden && el.id) {
    // Avature pattern: visible widget id=NNNN_input, hidden select id=NNNN
    const base = el.id.replace(/[-_](input|search|display|text)$/i, '');
    hidden = document.getElementById(base);
    if (hidden && hidden.tagName !== 'SELECT') hidden = null;
  }
  if (hidden && hidden.tagName === 'SELECT') {
    const opts = Array.from(hidden.options || []);
    let pick = opts.find(o => norm(o.text) === wantN || norm(o.value) === wantN);
    if (!pick) pick = opts.find(o => norm(o.text).includes(wantN) && wantN.length > 1);
    if (pick) {
      hidden.value = pick.value;
      hidden.selectedIndex = pick.index;
      fire(hidden);
      out.strategy = 'backing-hidden-select';
      return await finish(
        () => (hidden.options[hidden.selectedIndex] || {}).text || hidden.value,
        norm(pick.text));
    }
    // Same lie as the native-select path above: never invent an option. See
    // the long comment there - Wingtip's "School" was reported SET eight times
    // with a value its database does not contain.
    //
    // But an empty backing select does NOT mean "impossible": a remote-search
    // widget has nothing loaded until something is TYPED. So do what a human
    // does - type the query, wait for the server, and see what comes back. If
    // real results arrive we select one honestly; if none do, that is a true
    // "No results found" and we say so.
    const realOpts = opts.filter(o => (o.value || '').trim() !== '');
    if (realOpts.length === 0) {
      const priorInput = el.value || '';
      const cleanupTyping = () => {
        try {
          nativeSet(el, priorInput);
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Escape'}));
          el.blur();
        } catch (e) {}
      };
      typeInto(el, want);
      let fetched = [];
      for (let i = 0; i < 14; i++) {
        await sleep(160);
        fetched = Array.from(hidden.options || [])
                       .filter(o => (o.value || '').trim() !== '');
        if (fetched.length) break;
      }
      if (fetched.length) {
        let pick2 = fetched.find(o => norm(o.text) === wantN);
        if (!pick2) pick2 = fetched.find(o => norm(o.text).includes(wantN) && wantN.length > 2);
        if (pick2) {
          // Prefer clicking the rendered result so the widget's own handlers
          // commit the selection; fall back to setting the backing select.
          const li = findOptions().find(
            o => norm(o.innerText || o.textContent || '') === norm(pick2.text));
          if (li) { realClick(li); await sleep(120); }
          if (norm(hidden.value) !== norm(pick2.value)) {
            hidden.value = pick2.value;
            hidden.selectedIndex = pick2.index;
            fire(hidden);
          }
          out.strategy = 'remote-search-select2';
          return await finish(
            () => (hidden.options[hidden.selectedIndex] || {}).text || hidden.value,
            norm(pick2.text));
        }
        out.noMatch = true;
        out.availableOptions = fetched.map(o => (o.text || '').trim()).filter(Boolean);
        cleanupTyping();
        out.note = 'the search for ' + JSON.stringify(want) + ' returned results, ' +
                   'but none of them matched. The site offers: ' +
                   out.availableOptions.join(' / ');
        return JSON.stringify(out);
      }
      out.noMatch = true;
      out.emptyRemote = true;
      out.availableOptions = [];
      cleanupTyping();
      out.note = 'this is a search-as-you-type field and searching for ' +
                 JSON.stringify(want) + ' returned NO results. The value does ' +
                 'not exist in this site\'s list.';
      return JSON.stringify(out);
    }
    out.noMatch = true;
    out.availableOptions = realOpts.map(o => (o.text || '').trim()).filter(Boolean);
    out.note = 'no option matched ' + JSON.stringify(want) + '; the site offers: ' +
               out.availableOptions.join(' / ');
    return JSON.stringify(out);
  }

  // ---- combobox with no backing select: set the visible input ----
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
    nativeSet(el, want);
    fire(el);
    out.strategy = 'combobox-input-no-backing-select';
    return await finish(() => el.value, wantN);
  }

  out.note = 'matched an element that is not settable: ' + el.tagName;
  return JSON.stringify(out);
})
""".replace("__COLLECT__", _COLLECT_JS)


@dataclass
class FieldInfo:
    """One numbered, addressable control on the page."""

    index: int  # 1-based, what the agent sees
    label: str = ""
    meta: str = ""
    kind: str = ""
    sig: str = ""
    value: str = ""
    required: bool = False
    options: Optional[list[str]] = None
    checked: Optional[bool] = None

    def as_line(self) -> str:
        bits = [f"#{self.index}", f"{self.label or self.meta or '(unlabelled)'}"]
        detail = [self.kind]
        if self.checked is not None:
            detail.append('checked=' + str(self.checked).lower())
        if self.required:
            detail.append("required")
        if self.value:
            key = "option_value" if self.checked is not None else "currently"
            detail.append(f"{key}={self.value!r}")
        line = f"  {bits[0]:>4}  {bits[1]}  [{', '.join(detail)}]"
        if self.options:
            # Show them ALL. The agent picks a value from this list, so any
            # option hidden behind a "..." is one it will waste turns guessing
            # around - job570 cycled degree phrasings at a field whose correct
            # option was sitting at position 9. Long lists wrap instead.
            shown = " / ".join(self.options)
            if len(shown) > 900:
                shown = shown[:900] + " ...(truncated, ask for this field's options)"
            line += f"\n         options ({len(self.options)}): {shown}"
        return line

    def missing_required(self) -> bool:
        """Required and unanswered. Radios are left out: each button of a
        group is listed on its own, so one unchecked button says nothing."""
        if not self.required or self.kind == "radio":
            return False
        if self.kind == "checkbox":
            return not self.checked
        return not self.value.strip()


@dataclass
class ListFieldsResult:
    ok: bool
    fields: list[FieldInfo] = field(default_factory=list)
    note: str = ""
    # Controls on the page, including any beyond the 60 that are listed.
    total: int = 0

    def required_summary(self) -> str:
        """Say which required fields are still empty, or that none are.

        Southgate 1528 (2026-10-14) reached Avature's Work Experience step with
        every required box already filled from the saved profile, then spent
        its last 25 steps rewriting the Work History entries and never hit
        Submit. Telling the agent plainly that nothing required is missing
        lets it click Continue instead of auditing prefilled sections.
        """
        empty = [f for f in self.fields if f.missing_required()]
        more = self.total - len(self.fields)
        tail = (f" Only the first {len(self.fields)} of {self.total} controls are "
                "listed; check the rest of the page too.") if more > 0 else ""
        if empty:
            names = ", ".join(f"#{f.index} {f.label or f.meta or '(unlabelled)'}" for f in empty)
            return (f"Required and still EMPTY: {names}. Fill these. Leave fields that "
                    "already have a value alone unless they contradict a candidate fact." + tail)
        return ("Every required field listed above already has a value. Prefilled "
                "history entries (Work History, Education) stay as they are: do not "
                "rewrite them, click Continue / Next." + tail)

    def as_agent_message(self) -> str:
        if not self.ok or not self.fields:
            return (
                "FIELDS_LISTED_FAILED: could not read the form controls on this page"
                f"{' (' + self.note + ')' if self.note else ''}. Fill the field "
                "manually, or navigate to the form first and try again."
            )
        lines = [f"FIELDS_LISTED: {len(self.fields)} control(s) on the active page:"]
        lines.extend(f.as_line() for f in self.fields)
        lines.append("\n" + self.required_summary())
        lines.append(
            "\nSet one with its NUMBER, e.g. `NEED_FIELD: #3 = India`. The number "
            "is exact - no guessing, no wrong-field risk. One field per directive."
        )
        return "\n".join(lines)


@dataclass
class SetFieldResult:
    ok: bool
    # NOTE: deliberately NOT named `field` - that shadows dataclasses.field
    # inside the class body and breaks every default_factory below it.
    field_name: str = ""
    value: str = ""
    strategy: str = ""
    matched: str = ""
    readback: str = ""
    note: str = ""
    candidates: list[str] = field(default_factory=list)
    ambiguous: bool = False
    stale: bool = False
    reverted: bool = False
    # The site's own option list had nothing matching the requested value.
    no_match: bool = False
    # A search-as-you-type widget that returned no results at all.
    empty_remote: bool = False
    available_options: list[str] = field(default_factory=list)
    # Set when the answer book substituted an honest alternative phrasing.
    substituted: str = ""
    # The listed control was found again at this new number (rows inserted
    # above it since the listing); 0 when it was where it was listed.
    moved_to: int = 0

    def as_agent_message(self) -> str:
        moved = (f"\n\nNote: {FIELD_MOVED_NOTE}{self.moved_to}; numbers after it "
                 "may have moved too." if self.moved_to else "")
        if self.ok:
            sub = ""
            if self.substituted:
                sub = (
                    f"\n\nNote: this site's list had no {self.value!r}, so the closest "
                    f"TRUE option was chosen: {self.substituted!r}."
                )
            elif self.note:
                sub = f"\n\nNote: {self.note}."
            return (
                f"FIELD_SET: {self.matched or self.field_name} = {self.readback!r} "
                f"(via {self.strategy})"
                f"{sub}{moved}\n\n"
                "The value is set and verified in the DOM. Do NOT re-type or re-click "
                "this field. Continue with the remaining fields and submit."
            )
        lines = [f"FIELD_SET_FAILED: {self.note or 'could not set the field'}{moved}"]
        if self.no_match:
            # The site's vocabulary genuinely lacks this value, and we already
            # tried every honest alternative we know. Guessing more phrasings is
            # what burned job516's and job570's budgets - the answer was never
            # in the list. Show the real list once and make the agent choose or
            # give up.
            if self.available_options:
                lines.append(
                    "These are the ONLY values this field accepts, in full:"
                )
                lines.extend(f"  - {o}" for o in self.available_options[:60])
                lines.append(
                    "Pick the one that is TRUE for the candidate and set it by "
                    "number. If none of them is true, leave the field, submit what "
                    "you have, and report that this site's list has no valid "
                    "option. Do NOT invent a value and do NOT retry other spellings "
                    "- they are not in the list."
                )
            else:
                lines.append(
                    "This is a search-as-you-type field and the search returned "
                    "nothing, so there is no option to select. Try ONE more "
                    "spelling only if you have a genuinely different one; "
                    "otherwise leave it, submit what you have, and report this "
                    "field as the blocker."
                )
            return "\n".join(lines)
        if self.reverted:
            # This widget keeps its value in framework state and throws away
            # anything written programmatically. More attempts will not help -
            # Bluegum burned 15+ of them. Tell the agent to drive the widget by
            # hand once, then move on.
            lines.append(
                "This widget rejects programmatic values. Try ONCE by hand: click "
                "the control, type a few characters, wait for the dropdown, then "
                "click the option with browser_click on a fresh snapshot ref. If "
                "that also fails, leave the field, submit what you have, and "
                "report what blocked you. Do NOT retry this field repeatedly."
            )
            return "\n".join(lines)
        if self.ambiguous or self.stale:
            # Do NOT dump a prose candidate list here - re-describing the field
            # in more words is exactly the loop that burned nine rounds on a
            # live run. Push the agent onto numbers instead.
            lines.append(
                "Print `NEED_FIELDS` on its own line to get a numbered list of the "
                "form's controls, then set it by number: `NEED_FIELD: #N = <value>`."
            )
            return "\n".join(lines)
        if self.candidates:
            lines.append("Form controls visible on the page:")
            lines.extend(f"  - {c}" for c in self.candidates[:12])
            lines.append(
                "Print `NEED_FIELDS` for a numbered list and set it by number "
                "(`NEED_FIELD: #N = <value>`), or fill this one manually if it is "
                "a simple text box."
            )
        return "\n".join(lines)


class _Cdp:
    def __init__(self, ws_url: str):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed")
        self.ws = websocket.create_connection(
            ws_url, timeout=WS_TIMEOUT_SEC, suppress_origin=True
        )
        self._id = 0
        self.deadline: float | None = None

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        deadline = getattr(self, 'deadline', None)
        def remaining():
            if deadline is not None:
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError('widget operation deadline exceeded')
                self.ws.settimeout(min(WS_TIMEOUT_SEC, seconds))
        remaining()
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            remaining()
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
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
        logger.warning("set_field: could not list targets: %s", e)
        return []
    pages = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"]
    browser_session.note_tabs(cdp_url, pages)
    return [t for t in pages if t.get("webSocketDebuggerUrl")]


def _ensure_browser(cdp_url: str) -> tuple[bool, str]:
    """Confirm the browser is alive, relaunching it if it died mid-job.

    `browser_session.ensure_healthy()` guards the runner loop BETWEEN jobs, but
    Chromium exits when its last page target closes, which happens *during* a
    job too (job99997 and job484 both died mid-form, and a diagnostic run
    killed it the same way). Without this, every subsequent handoff in the same
    job fails against a dead endpoint and the agent burns its whole budget
    reporting confusing DOM errors.

    Returns (alive, note).
    """
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=3):
            return True, ""
    except Exception:
        pass
    logger.warning("form_filler: CDP unreachable at %s; attempting recovery.", cdp_url)
    try:
        from module4 import browser_session, constants

        recovered = browser_session.ensure_healthy(constants.browser_profile_dir())
    except Exception as e:  # pragma: no cover - recovery is best-effort
        return False, f"browser is down and could not be recovered ({e})"
    if not recovered:
        return False, "browser is down and could not be restarted"
    logger.info("form_filler: browser recovered at %s", recovered)
    return True, "browser was restarted"


def list_fields(cdp_url: str) -> ListFieldsResult:
    """Enumerate the active page's form controls, numbered for the agent.

    This is the mechanism that removes prose matching entirely: the agent picks
    a NUMBER off this list instead of describing a field in words that then
    have to be interpreted.
    """
    alive, note = _ensure_browser(cdp_url)
    if not alive:
        return ListFieldsResult(ok=False, note=note)
    targets = _page_targets(cdp_url)
    if not targets:
        return ListFieldsResult(ok=False, note="no page target")
    try:
        with _Cdp(targets[0]["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Runtime.enable")
            r = cdp.call(
                "Runtime.evaluate",
                {"expression": _LIST_FIELDS_JS, "returnByValue": True, "awaitPromise": False},
            )
            raw = r.get("result", {}).get("result", {}).get("value")
            if not raw:
                return ListFieldsResult(ok=False, note="page returned no controls")
            data = json.loads(raw)
    except Exception as e:
        logger.warning("list_fields failed: %s", e)
        return ListFieldsResult(ok=False, note=str(e)[:120])

    fields = [
        FieldInfo(
            index=i + 1,
            label=f.get("label") or "",
            meta=f.get("meta") or "",
            kind=f.get("kind") or "",
            sig=f.get("sig") or "",
            value=str(f.get("value") or ""),
            required=bool(f.get("required")),
            options=f.get("options") or None,
            checked=f.get("checked"),
        )
        for i, f in enumerate(data.get("fields") or [])
    ]
    logger.info("list_fields: %d control(s) on the active page", len(fields))
    return ListFieldsResult(ok=bool(fields), fields=fields, total=int(data.get("total") or len(fields)))


# Said when an indexed set found its control at a new number (agent_driver
# re-lists the form for the agent when it sees this).
FIELD_MOVED_NOTE = "the form changed and this field moved to #"

# Signatures from the most recent list_fields call, per CDP endpoint, so a
# `NEED_FIELD: #3` can be validated against the list the agent actually saw.
_LAST_LISTING: dict[str, list[str]] = {}
# (kind, label) per listed control, so the driver can tell a password field
# from the number the agent uses (agent_driver._enforce_account_password).
_LAST_KINDS: dict[str, list[tuple[str, str]]] = {}


def remember_listing(cdp_url: str, result: ListFieldsResult) -> None:
    _LAST_LISTING[cdp_url] = [f.sig for f in result.fields]
    _LAST_KINDS[cdp_url] = [(f.kind, f.label or f.meta) for f in result.fields]


def listed_field(cdp_url: str, index: Optional[int]) -> Optional[tuple[str, str]]:
    """(kind, label) of control #index from the latest listing, if known."""
    rows = _LAST_KINDS.get(cdp_url) or []
    if index is None or not 1 <= index <= len(rows):
        return None
    return rows[index - 1]


def set_field(
    cdp_url: str, field_hint: str, value: str, index: Optional[int] = None
) -> SetFieldResult:
    """Set one form field deterministically.

    `index` (1-based) addresses a control by NUMBER from `list_fields`, which
    involves no interpretation at all and is the preferred path. When an index
    is given the value is checked against the signature the agent was shown, so
    a form that rewrote itself between listing and setting is refused rather
    than mis-filled.

    A prose `field_hint` still works but must resolve CONFIDENTLY (see the
    scoring floor and runner-up margin in the JS) - an ambiguous description is
    refused with instructions to use a number instead. Guessing here is what
    put "Springfield Institute of Technology" into a Full name box while reporting success.

    Only the ACTIVE/frontmost page is searched by default. Searching every tab
    is dangerous for form fills in a way it is not for uploads: a stale
    background tab (a LinkedIn page, a previous ATS) can contain a field whose
    label loosely matches the hint, and we would silently fill the wrong form.
    Observed 2026-09-04: a 'Country/Region Code' request matched a 'Zip Code'
    field on a leftover tab and reported success.

    Fallback: if no control matches on the active page, other tabs are tried,
    but only accepting a STRONG match, never a loose token-overlap guess.
    Indexed requests never fall back to another tab - a number refers to the
    list the agent was just shown, which came from the active page.
    """
    alive, note = _ensure_browser(cdp_url)
    if not alive:
        return SetFieldResult(ok=False, field_name=field_hint, value=value, note=note)

    targets = _page_targets(cdp_url)
    if not targets:
        return SetFieldResult(ok=False, field_name=field_hint, value=value, note="no page target")

    by_index = None if index is None else index - 1
    expect_sig = ""
    if by_index is not None:
        sigs = _LAST_LISTING.get(cdp_url) or []
        if 0 <= by_index < len(sigs):
            expect_sig = sigs[by_index]

    def attempt_value(
        tab: dict, strict: bool, use_value: str
    ) -> Optional[SetFieldResult]:
        try:
            with _Cdp(tab["webSocketDebuggerUrl"]) as cdp:
                cdp.call("Runtime.enable")
                r = cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": f"({_SET_FIELD_JS})({json.dumps(field_hint)},"
                        f" {json.dumps(use_value)}, {json.dumps(bool(strict))},"
                        f" {json.dumps(by_index)}, {json.dumps(expect_sig)})",
                        "returnByValue": True,
                        # The setter is async now: it settles and re-reads to
                        # catch framework-reverted values, and drives
                        # react-select widgets with real waits.
                        "awaitPromise": True,
                    },
                )
                raw = r.get("result", {}).get("result", {}).get("value")
                if not raw:
                    return None
                data = json.loads(raw)
                if data.get('widgetToken'):
                    from module4.form_widgets import drive_widget
                    data = drive_widget(cdp, data['widgetToken'], data)
                return SetFieldResult(
                    ok=bool(data.get("ok")),
                    field_name=field_hint,
                    value=use_value,
                    strategy=data.get("strategy") or "",
                    matched=data.get("matched") or "",
                    readback=str(data.get("readback") or ""),
                    note=data.get("note") or "",
                    candidates=data.get("candidates") or [],
                    ambiguous=bool(data.get("ambiguous")),
                    stale=bool(data.get("stale")),
                    reverted=bool(data.get("reverted")),
                    no_match=bool(data.get("noMatch")),
                    empty_remote=bool(data.get("emptyRemote")),
                    available_options=data.get("availableOptions") or [],
                    moved_to=int(data.get("movedTo") or 0),
                )
        except Exception as e:
            logger.debug("set_field: target failed: %s", e)
            return None

    def attempt(tab: dict, strict: bool) -> Optional[SetFieldResult]:
        return attempt_value(tab, strict, value)

    # 1. The active page (CDP lists the frontmost target first).
    primary = attempt(targets[0], strict=False)
    if primary and primary.ok:
        logger.info(
            "set_field: %s = %r via %s (matched=%r readback=%r)",
            f"#{index}" if index else repr(field_hint),
            value,
            primary.strategy,
            primary.matched,
            primary.readback,
        )
        return primary

    # 1b. The site's list has nothing matching what was asked for. Rather than
    # handing that back and letting the agent burn a turn per guess (job516
    # cycled three spellings of the same college, job570 two of the same
    # degree), walk our own ranked list of phrasings that are still TRUE for
    # the candidate and try the first one this site actually offers.
    if primary is not None and primary.no_match:
        try:
            from module4 import answer_book
        except Exception:  # pragma: no cover - import guard
            answer_book = None  # type: ignore[assignment]
        tried = {_norm_py(value)}

        def try_alt(alt: str) -> Optional[SetFieldResult]:
            tried.add(_norm_py(alt))
            logger.info("set_field: %r not offered; trying honest alternative %r", value, alt)
            retry = attempt_value(targets[0], False, alt)
            if retry and retry.ok:
                retry.value = value
                retry.substituted = alt
                logger.info(
                    "set_field: %s = %r (substituted %r) via %s",
                    f"#{index}" if index else repr(field_hint),
                    value,
                    alt,
                    retry.strategy,
                )
            return retry

        if answer_book is not None and primary.available_options:
            alt = answer_book.pick_from_options(value, primary.available_options)
            if alt and _norm_py(alt) not in tried:
                retry = try_alt(alt)
                if retry and retry.ok:
                    return retry

        # 1c. A search box (Workday School) only shows what the query matched,
        # so "not offered" means "not found by THIS search". Search our other
        # true spellings and the escapes ("Other - See Resume") here, in code.
        # J&J 1885 (2026-10-18) left this to the agent: ~25 model steps of
        # typed spellings on one School box, then out of budget two pages on.
        search_box = primary.empty_remote or primary.strategy.startswith("workday-prompt")
        if answer_book is not None and search_box:
            for query in answer_book.search_queries_for(value)[:MAX_SEARCH_FALLBACKS]:
                if _norm_py(query) in tried:
                    continue
                res = try_alt(query)
                if res and res.ok:
                    return res
                if res and res.no_match and res.available_options:
                    pick = answer_book.pick_from_options(value, res.available_options)
                    if pick and _norm_py(pick) not in tried:
                        res = try_alt(pick)
                        if res and res.ok:
                            return res

    # An indexed request must never wander to another tab: the number refers to
    # the listing taken from the active page.
    if by_index is not None:
        return primary or SetFieldResult(
            ok=False,
            field_name=field_hint,
            value=value,
            note="could not set that field number on the active page",
        )

    # 2. Other tabs, strict matching only.
    for tab in targets[1:]:
        res = attempt(tab, strict=True)
        if res and res.ok:
            logger.info(
                "set_field: %r = %r on a background tab via %s",
                field_hint,
                value,
                res.strategy,
            )
            return res

    return primary or SetFieldResult(
        ok=False, field_name=field_hint, value=value, note="no tab could set the field"
    )


# `NEED_FIELD: #3 = India` (preferred) or `NEED_FIELD: College Name = Springfield Institute of Technology`.
_INDEX_RE = re.compile(r"^#?[ \t]*(\d{1,3})$")
_FIELD_REQUEST_RE = re.compile(
    r"^[ \t]*NEED_FIELD:[ \t]*(?P<f>[^ \t\r\n=][^\r\n=]*?)[ \t]*=[ \t]*(?P<v>[^\r\n]*)\r?$",
    re.MULTILINE | re.I,
)


def parse_request(line: str) -> Optional[tuple[str, str, Optional[int]]]:
    """Parse one directive line; trailing transcript/diagnostic lines are not values."""
    m = _FIELD_REQUEST_RE.match(line)
    if not m:
        return None
    fieldname = m.group("f").strip(" \t")
    value = m.group("v").strip(" \t")
    # Only exact quoted-empty literals mean clear. Other quotes are literal.
    if value in ('""', "''"):
        value = ""
    if not fieldname:
        return None
    idx_m = _INDEX_RE.match(fieldname)
    if idx_m:
        n = int(idx_m.group(1))
        if n >= 1:
            return fieldname, value, n
    return fieldname, value, None
