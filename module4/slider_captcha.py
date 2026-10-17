"""Solve on-page jigsaw "Slide to verify" captchas in code (NEED_SLIDER handoff).

Redpine 1485 (2026-10-11) died on one of these: the agent spent 8.6 minutes and
the rest of its step budget trying to eyeball the gap with browser_vision and
dragging with hand-built CDP mouse events, and failed both drags.

The widget (the open-source `slider-captcha` family: `.slider-captcha-stage`
with three canvases, `.slider-captcha-thumb`, `.slider-captcha-status`) is
checked entirely in the page, with no request to any server:
    |thumb_offset - targetX| <= tolerance (5 px on Redpine).
Both pieces of geometry are readable from the page itself:
  * the cutout canvas has the gap outlined at targetX (pixel alpha differs
    from the dark overlay around it);
  * the piece canvas holds the piece drawn at targetX and is displayed with
    `translateX(thumb - targetX)`, so its on-screen rect gives where the piece
    sits now.
drag distance = gap's left edge on screen - piece's left edge on screen.
The transform (`translateX(-targetX)` at rest) is used as a cross-check.

The drag itself is real CDP mouse input on the thumb (press, ~25 eased moves
with small jitter, release), the same input a person produces. Measured on
Redpine's live form in a scratch browser: 5/5 verified, about 5 s each.

Only canvas jigsaws whose check runs in the page are handled here. Bot-wall
sliders (DataDome's "Slide right to secure your access") judge the browser and
network, not the drag; they are handled by the jobo-browser plugin
(hermes_plugin/jobo-browser/captcha_guard.py), never by this module.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field

from module4 import clicker

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
DRAG_STEPS = 25
# Estimates further apart than this mean one of them misread the widget.
AGREE_PX = 4.0

_FIND_JS = r"""
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0; };
  const thumbs = Array.from(document.querySelectorAll('.slider-captcha-thumb')).filter(vis);
  return {count: thumbs.length};
})()
"""

# Scroll the widget into view and measure. Returns screen geometry only; the
# drag is done with trusted input from Python.
_MEASURE_JS = r"""
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0; };
  const thumb = Array.from(document.querySelectorAll('.slider-captcha-thumb')).find(vis);
  if (!thumb) return {ok: false, note: 'no visible slider-captcha thumb on this page'};
  let root = thumb.parentElement;
  while (root && !root.querySelector('.slider-captcha-stage')) root = root.parentElement;
  const stage = root && root.querySelector('.slider-captcha-stage');
  if (!stage) return {ok: false, note: 'slider thumb has no .slider-captcha-stage next to it'};
  const status = root.querySelector('.slider-captcha-status');
  const statusText = status ? (status.innerText || '').trim() : '';
  if (status && /\bok\b/.test(status.className))
    return {ok: true, already: true, status: statusText};
  stage.scrollIntoView({block: 'center', inline: 'nearest'});
  thumb.scrollIntoView({block: 'nearest', inline: 'nearest'});
  const canvases = Array.from(stage.querySelectorAll('canvas')).filter(vis);
  if (canvases.length < 2)
    return {ok: false, note: 'slider uses images, not canvases (not supported yet)'};

  const info = canvases.map(c => {
    const r = c.getBoundingClientRect();
    const out = {rect: {left: r.left, width: r.width}, w: c.width, h: c.height,
                 transform: c.style.transform || '', tainted: false};
    try {
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      const hist = new Array(256).fill(0);
      let opaque = 0, minX = Infinity;
      for (let i = 3; i < d.length; i += 4) {
        const a = d[i]; hist[a]++;
        if (a > 0) { opaque++; const x = ((i - 3) / 4) % c.width; if (x < minX) minX = x; }
      }
      let mode = 0; for (let a = 1; a < 256; a++) if (hist[a] > hist[mode]) mode = a;
      // Columns where alpha departs from the overlay's own alpha = the gap outline.
      let gapMin = Infinity;
      if (mode > 0 && mode < 250) {
        for (let x = 0; x < c.width && gapMin === Infinity; x++) {
          let n = 0;
          for (let y = 0; y < c.height; y++) {
            const a = d[(y * c.width + x) * 4 + 3];
            if (Math.abs(a - mode) > 20) n++;
          }
          if (n > 3) gapMin = x;
        }
      }
      Object.assign(out, {opaqueFrac: opaque / (c.width * c.height), minX, mode, gapMin});
    } catch (e) { out.tainted = true; }
    return out;
  });
  const piece = info.find(c => !c.tainted && c.opaqueFrac > 0 && c.opaqueFrac < 0.3)
             || info.find(c => /translateX/.test(c.transform));
  const cut = info.find(c => !c.tainted && c.mode > 0 && c.mode < 250 && isFinite(c.gapMin));
  const tr = t => { const m = /translateX\((-?[\d.]+)px\)/.exec(t || ''); return m ? parseFloat(m[1]) : null; };
  const thumbTx = tr(thumb.style.transform) || 0;
  let cvDist = null, txDist = null;
  if (piece && cut && isFinite(piece.minX)) {
    const ps = piece.rect.width / piece.w, cs = cut.rect.width / cut.w;
    cvDist = (cut.rect.left + cut.gapMin * cs) - (piece.rect.left + piece.minX * ps);
  }
  const ptx = piece ? tr(piece.transform) : null;
  if (ptx !== null) txDist = -ptx;  // translateX(thumb - targetX)
  const tb = thumb.getBoundingClientRect();
  return {ok: true, already: false, status: statusText, cvDist, txDist, thumbTx,
          x: tb.left + tb.width / 2, y: tb.top + tb.height / 2,
          tainted: info.some(c => c.tainted)};
})()
"""

_RESULT_JS = r"""
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
                      return r.width > 0 && r.height > 0; };
  const thumb = Array.from(document.querySelectorAll('.slider-captcha-thumb')).find(vis);
  let root = thumb && thumb.parentElement;
  while (root && !root.querySelector('.slider-captcha-status')) root = root.parentElement;
  const status = root && root.querySelector('.slider-captcha-status');
  const tokens = Array.from(document.querySelectorAll('input[type=hidden]'))
    .filter(i => /captcha/i.test(i.name || i.id || '')).map(i => (i.value || '').length);
  return {status: status ? (status.innerText || '').trim() : '',
          ok: !!(status && /\bok\b/.test(status.className)), tokens};
})()
"""


@dataclass
class SliderResult:
    ok: bool
    note: str = ""
    attempts: int = 0
    log: list[str] = field(default_factory=list)

    def as_agent_message(self) -> str:
        if self.ok:
            return (
                f"SLIDER_SOLVED: {self.note}\n\n"
                "The slide-to-verify check on this page now shows verified. Do not touch "
                "the slider again (dragging it resets it). Finish any remaining required "
                "fields and submit the application."
            )
        return (
            f"SLIDER_FAILED: {self.note}\n\n"
            "Do not try to drag the slider yourself and do not ask NEED_SLIDER again. "
            "If the form cannot be submitted without it, end now with STATUS: "
            "captcha_failed and this reason."
        )


def _value(resp: dict):
    return resp["result"]["result"].get("value")


def _eval(cdp, expr: str):
    return _value(cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}))


def _drag(cdp, x: float, y: float, dist: float) -> None:
    mouse = lambda **p: cdp.call("Input.dispatchMouseEvent", p)  # noqa: E731
    mouse(type="mouseMoved", x=x - 30, y=y + 12)
    time.sleep(0.05)
    mouse(type="mouseMoved", x=x, y=y)
    time.sleep(0.12)
    mouse(type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
    time.sleep(random.uniform(0.08, 0.15))
    for i in range(1, DRAG_STEPS + 1):
        f = i / DRAG_STEPS
        ease = 1 - (1 - f) ** 3
        mouse(type="mouseMoved", x=x + dist * ease + random.uniform(-0.4, 0.4),
              y=y + random.uniform(-1.0, 1.0), button="left", buttons=1)
        time.sleep(random.uniform(0.012, 0.03))
    mouse(type="mouseMoved", x=x + dist, y=y, button="left", buttons=1)
    time.sleep(0.12)
    mouse(type="mouseReleased", x=x + dist, y=y, button="left", buttons=0, clickCount=1)


def _pick_distance(m: dict) -> tuple[float | None, str]:
    cv, tx = m.get("cvDist"), m.get("txDist")
    if cv is not None and tx is not None:
        if abs(cv - tx) <= AGREE_PX:
            return cv, f"gap {cv:.0f}px (canvas and transform agree)"
        return tx, f"gap {tx:.0f}px from the piece transform (canvas said {cv:.0f}px)"
    if cv is not None:
        return cv, f"gap {cv:.0f}px from the canvas"
    if tx is not None:
        return tx, f"gap {tx:.0f}px from the piece transform"
    return None, "could not locate the gap (canvas unreadable and no piece transform)"


def _target_with_slider(cdp_url: str) -> dict | None:
    for t in clicker._page_targets(cdp_url):
        try:
            with clicker._Cdp(t["webSocketDebuggerUrl"]) as cdp:
                if (_eval(cdp, _FIND_JS) or {}).get("count"):
                    return t
        except Exception as e:  # a dead or busy tab is not the one we want
            logger.debug("slider: target %s skipped: %s", t.get("id"), e)
    return None


def solve(cdp_url: str) -> SliderResult:
    """Find the page's jigsaw slider and drag it onto the gap. Never submits."""
    from module4 import form_filler

    alive, note = form_filler._ensure_browser(cdp_url)
    if not alive:
        return SliderResult(ok=False, note=note)
    target = _target_with_slider(cdp_url)
    if target is None:
        return SliderResult(ok=False, note=(
            "no supported slide-to-verify jigsaw on any open tab (only the in-page "
            "canvas 'slider-captcha' widget is supported; DataDome and similar bot "
            "walls are not)"))
    res = SliderResult(ok=False)
    with clicker._Cdp(target["webSocketDebuggerUrl"]) as cdp:
        cdp.call("Runtime.enable")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            res.attempts = attempt
            m = _eval(cdp, _MEASURE_JS) or {}
            if not m.get("ok"):
                res.note = m.get("note") or "slider could not be measured"
                return res
            if m.get("already"):
                res.ok, res.note = True, f"already verified ({m.get('status')!r})"
                return res
            time.sleep(0.35)  # let the scroll settle, then re-measure for screen coords
            m = _eval(cdp, _MEASURE_JS) or {}
            dist, how = _pick_distance(m)
            if dist is None:
                res.note = how
                return res
            # Both estimates are measured from where the piece is drawn NOW, so
            # they are already the remaining distance (no thumb offset to add).
            res.log.append(f"attempt {attempt}: {how}, drag {dist:.0f}px")
            _drag(cdp, float(m["x"]), float(m["y"]), dist)
            time.sleep(0.8)
            r = _eval(cdp, _RESULT_JS) or {}
            if r.get("ok"):
                res.ok = True
                res.note = f"verified on attempt {attempt} ({how}; widget says {r.get('status')!r})"
                logger.info("slider: %s", "; ".join(res.log))
                return res
            res.log.append(f"  widget said {r.get('status')!r}")
            time.sleep(1.0)  # failed drags reset the piece; a 3rd failure redraws the puzzle
    res.note = f"not verified after {res.attempts} drags ({'; '.join(res.log)[-300:]})"
    logger.warning("slider: %s", res.note)
    return res


def parse_request(text: str) -> bool:
    return any(line.strip().upper().startswith("NEED_SLIDER") for line in (text or "").splitlines())


__all__ = ["SliderResult", "solve", "parse_request", "json"]
