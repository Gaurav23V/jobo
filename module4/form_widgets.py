"""Exact-node widget gestures: bounded CDP input, never text/checked injection.

The form resolver hands off its selected node (not a second label search).
An async generator yields hit-tested UI gestures and finally a settled verdict.
"""
import json
import time

WIDGET_TIMEOUT_SEC = 35

WIDGET_JS = r"""
(function(state) {
  const {el, want, out, kind} = state;
  const norm = s => String(s ?? '').trim().toLowerCase().replace(/\s+/g, ' ');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const visible = n => n?.isConnected && n.getBoundingClientRect().width > 0 &&
    n.getBoundingClientRect().height > 0 && n.checkVisibility({checkVisibilityCSS:true});
  const enabled = n => n && !n.matches(':disabled,[aria-disabled=true], [class*="--disabled"]') && !n.closest('[inert]');
  const fail = note => ({...out, ok:false, note});
  const deadline=performance.now()+34000;
  const choiceSelector='input[type=radio],input[type=checkbox]';
  const nearestChoice=n=>n.parentElement?.closest('[role=radio],[role=checkbox]');
  const candidate=['radio','checkbox'].includes(kind) ? nearestChoice(el) : null;
  const wrapper=candidate && candidate.getAttribute('role')===kind &&
    candidate.querySelectorAll(choiceSelector).length===1 && candidate.querySelector(choiceSelector)===el ? candidate : null;
  const wrapperRole=wrapper?.getAttribute('role'), wrapperValue=wrapper?.getAttribute('data-value');
  const wrapperValid=()=>!wrapper || (wrapper.isConnected && enabled(wrapper) && nearestChoice(el)===wrapper &&
    wrapper.getAttribute('role')===wrapperRole && wrapper.getAttribute('data-value')===wrapperValue &&
    wrapper.querySelectorAll(choiceSelector).length===1 && wrapper.querySelector(choiceSelector)===el);
  const valid=()=>performance.now()<deadline && state.valid() && enabled(el) && wrapperValid() &&
    (!pending || ownsSurface(pending.node,pending.s));
  const invalid=()=>fail('exact widget identity changed, detached, disabled or operation timed out');
  let pending=null;
  const ownsSurface=(node,s)=>s===node || s.control===node || (node===el && s===wrapper && wrapperValid());
  // Angular Material floats a long question label over its own select
  // trigger. A hit on that label is the same control: the label sits in the
  // same mat-form-field and that field holds no other control.
  const ownLabelHit=(s,top)=>{
    if(kind!=='bare-combobox' || s!==el || !top) return false;
    const lab=top.closest('mat-label,label');
    const field=el.closest('mat-form-field,[data-automation-id^=formField]');
    if(!lab || !field || !field.contains(lab)) return false;
    const ctl=field.querySelectorAll('input:not([type=hidden]),select,textarea,[role=combobox],button[aria-haspopup=listbox]');
    return [...ctl].every(n=>n===el || el.contains(n));
  };
  // A Workday prompt's picked pills sit over its own search box; a hit on a
  // pill area inside the same prompt container focuses that same search box.
  const ownPromptHit=(s,top)=>{
    if(kind!=='workday-prompt' || s!==el || !top) return false;
    const box=el.closest('[data-automation-id=multiselectInputContainer]');
    return !!box && box.contains(top) && !top.closest('[data-automation-id=DELETE_charm]');
  };
  const hitOwns=(s,top)=>!!top && (top===s || s.contains(top) || ownLabelHit(s,top) || ownPromptHit(s,top));
  const ownsHit=(node,top)=>{
    const interactive=top.closest('a,button,input,select,textarea,[role=button]');
    const label=top.closest('label');
    return (!interactive || interactive===node) && (!label || label.control===node);
  };
  const nodeIdentity=n=>[n.tagName,n.type,n.id,n.name,n.form,n.getRootNode(),n.getAttribute('role'),
    n.matches('input[type=radio],input[type=checkbox]') ? n.value : null,
    n!==el ? n.textContent : null];
  const guard=()=>{
    if(!valid()||!pending) return false;
    const {node,s,x,y,key,check,identity}=pending;
    if(!node.isConnected||!enabled(node)||!visible(s)||!nodeIdentity(node).every((v,i)=>v===identity[i])) return false;
    if(key && node.getRootNode().activeElement!==node) return false;
    let top=document.elementFromPoint(x,y);
    while(top?.shadowRoot) {const deep=top.shadowRoot.elementFromPoint(x,y);if(!deep||deep===top)break;top=deep;}
    return hitOwns(s,top) && (!check || ownsHit(node,top));
  };
  const finish = async (read, expected) => {
    await sleep(650);
    if(!valid()) return invalid();
    const value = read();
    return {...out, ok:value !== null && norm(value)===norm(expected),
      readback:value === null ? '' : String(value), reverted:value === null || norm(value)!==norm(expected),
      note:value !== null && norm(value)===norm(expected) ? '' : 'widget selection did not persist on the exact control'};
  };
  async function* run() {
    if (!valid()) return invalid();
    if (kind==='radio' || kind==='checkbox') {
      let desired;
      const w=norm(want);
      if (w==='true' || w==='checked') desired=true;
      else if (w==='false' || w==='unchecked') desired=false;
      else if (w && (w===norm(el.value) || w===norm(state.label))) desired=true;
      else if (kind==='checkbox' && ['yes','no'].includes(w)) desired=w==='yes';
      else return fail('use true/false or this exact option label/value; no other radio will be chosen');
      out.strategy='trusted-'+kind;
      if (el.checked!==desired || el.indeterminate) {
        if (kind==='radio' && !desired) return fail('cannot uncheck a selected radio with a click; choose the intended sibling by its own number');
        // Avature consent boxes on the 2026-10-15 form workers never received
        // a trusted click at all (GHO-23). Only then, the element's own click()
        // runs the same activation (toggle + input/change), once, on this exact
        // box. A click the page received and undid is the page's answer.
        let arrived=false;
        const seen=e=>{ if(e.isTrusted && (e.target===el || e.target.control===el)) arrived=true; };
        window.addEventListener('click',seen,true);
        try { yield {node:el, check:true}; if (el.checked!==desired) await sleep(300); }
        finally { window.removeEventListener('click',seen,true); }
        if (!arrived && valid() && el.checked!==desired && !el.indeterminate) {
          el.click();
          out.strategy+='+dom-click';
        }
      }
      return await finish(()=>{
        const aria=wrapper?.getAttribute('aria-checked');
        return el.indeterminate || (aria!=null && aria!==String(el.checked)) ? null : String(el.checked);
      },String(desired));
    }
    if (kind==='bare-combobox') {
      out.strategy='trusted-listbox';
      // Shine's select-field links nothing: no aria-controls, and its
      // <ul role=listbox> has no id (job1455 "Current Salary", 2026-10-04 and
      // -11: every pick came back "no matching option"). Accept such a list
      // only if it was not open before our gesture and sits in a wrapper of
      // this control that holds no other combobox.
      const preOpen=new Set();
      const unlinked=()=>{
        for(let n=el.parentElement,h=0;n&&n!==document.body&&h<3;n=n.parentElement,h++) {
          if([...n.querySelectorAll('[role=combobox]')].some(c=>c!==el && !el.contains(c))) return [];
          const lists=[...n.querySelectorAll('[role=listbox]')].filter(l=>visible(l)&&!preOpen.has(l));
          if(lists.length) return lists.length===1 ? lists : [];
        }
        return [];
      };
      const roots=()=>{
        const ids=[...(el.getAttribute('aria-controls')||'').split(/\s+/),
          ...(el.getAttribute('aria-owns')||'').split(/\s+/), el.id ? el.id+'-list':''];
        const linked=[...new Set(ids.filter(Boolean).map(id=>el.getRootNode().getElementById(id)))]
          .filter(n=>n && n.matches('[role=listbox]') && visible(n));
        return linked.length ? linked : unlinked();
      };
      if (!norm(want)) return fail('empty selection query; no option selected');
      el.getRootNode().querySelectorAll('[role=listbox]').forEach(l=>{if(visible(l)) preOpen.add(l);});
      const beforeText=norm(el.innerText||el.textContent);
      yield {node:el};
      let options=[];
      for(let i=0;i<16;i++) {
        await sleep(140);
        options=roots().flatMap(r=>[...r.querySelectorAll('[role=option]')]).filter(n=>visible(n)&&enabled(n));
        if(options.length) break;
      }
      const matches=options.filter(n=>norm(n.innerText||n.textContent)===norm(want));
      if(matches.length!==1) {
        const offered=options.map(n=>(n.innerText||n.textContent).trim());
        // Close the list we opened, or it covers the next field (Workday and
        // Angular both close on Escape).
        if(roots().length) { yield {node:el, key:'Escape'}; await sleep(250); }
        // Selects that ignore Escape close when their own trigger is clicked again.
        if(roots().length) { yield {node:el}; await sleep(250); }
        return {...fail(matches.length ? 'ambiguous exact options' : 'no matching option in this control\'s own list'),
          noMatch:true, emptyRemote:options.length===0, availableOptions:offered};
      }
      const text=(matches[0].innerText||matches[0].textContent).trim();
      const pick=matches[0], list=pick.closest('[role=listbox]');
      let committedEvent=false;
      const observed=ev=>{if(ev.isTrusted) committedEvent=true;};
      el.addEventListener('input',observed);el.addEventListener('change',observed);
      try {
        yield {node:pick};
        return await finish(()=>{
          const current=roots().flatMap(r=>[...r.querySelectorAll('[role=option]')]);
          const exact=current.filter(n=>norm(n.innerText||n.textContent)===norm(text));
          const exposed=exact.length ? exact : pick.isConnected ? [pick] : [];
          // Exposed selection state is authoritative, never overruled by display.
          if(exposed.some(n=>n.getAttribute('aria-selected')==='false')) return null;
          if(exposed.some(n=>n.getAttribute('aria-selected')==='true')) return text;
          if(current.some(n=>n.getAttribute('aria-selected')==='true' && norm(n.textContent)!==norm(text))) return null;
          const display=el.innerText||el.textContent;
          const closed=!visible(list) || el.getAttribute('aria-expanded')==='false';
          return norm(display)===norm(text) && ((beforeText!==norm(display)&&closed)||committedEvent) ? display : null;
        },text);
      } finally {el.removeEventListener('input',observed);el.removeEventListener('change',observed);}
    }
    if (kind==='workday-prompt') {
      // Workday search prompt ("How Did You Hear About Us?", Skills, School):
      // a pick is a PILL in the field, not text in the box. Type the query,
      // press Enter to search, click the exact matching result, confirm the
      // pill, then close the results list. Clicking a result toggles it, so
      // an already-picked value is left alone.
      out.strategy='workday-prompt';
      const box=el.closest('[data-automation-id=multiselectInputContainer]') || el.parentElement;
      const pills=()=>[...box.querySelectorAll('[data-automation-id=selectedItem]')]
        .map(n=>(n.getAttribute('title')||n.innerText||'').trim());
      const wantN=norm(want);
      if(!wantN) return fail('empty selection query; no option selected');
      const already=pills().find(t=>norm(t)===wantN);
      if(already) return {...out, ok:true, readback:already, note:''};
      const lists=()=>[...document.querySelectorAll('[data-automation-id=activeListContainer]')].filter(visible);
      // Workday always adds a row that just repeats the typed text; its id is
      // "menuItem-" + exactly what was typed, while real catalog results carry
      // an opaque id. Clicking it either invents a value (Wolters Kluwer added
      // the skill "Zzzqx Nonsense") or silently does nothing (GEA). Never pick it.
      const isEcho=n=>{const row=n.closest('[role=option]');return !!row && row.id==='menuItem-'+String(want);};
      const leaves=()=>lists().flatMap(l=>[...l.querySelectorAll('[data-automation-id=promptLeafNode]')])
        .filter(n=>visible(n)&&!isEcho(n)&&!/^no items\.?$/i.test((n.innerText||'').trim()));
      // Close the results list so it cannot cover the next field. Workday
      // ignores Escape and blur(); Tab closes it, but on free-text prompts
      // (GEA Skills) Tab also turns leftover search text into a pill. So empty
      // the search box first, then Tab out.
      async function* closeList() {
        if(!lists().length && !el.value) return;
        if(el.getRootNode().activeElement!==el) el.focus();
        if(el.value) { yield {node:el, key:'type', text:'', clear:true, noEnter:true}; await sleep(250); }
        if(lists().length) { yield {node:el, key:'Tab'}; await sleep(400); }
      }
      const leafText=n=>(n.querySelector('[data-automation-id=promptOption]')?.innerText||n.innerText||'').trim();
      const pillsBefore=new Set(pills().map(norm));
      yield {node:el};
      await sleep(150);
      if(el.getRootNode().activeElement!==el) el.focus();
      // Clear whatever search text is left from an earlier pick, then type.
      yield {node:el, key:'type', text:want, clear:true};
      let found=[];
      for(let i=0;i<30;i++) {
        await sleep(150);
        // Free-text prompts (GEA Skills) have no catalog: Enter itself saves
        // the typed text as a pill. That is the site accepting the value, not
        // us inventing one, but say so plainly instead of calling it a pick.
        const typed=pills().find(t=>norm(t)===wantN && !pillsBefore.has(norm(t)));
        if(typed) {
          yield* closeList();
          return {...out, ok:true, strategy:'workday-prompt-freetext', readback:typed,
            note:'this field takes free text; it was saved exactly as typed (not picked from a list)'};
        }
        found=leaves();
        if(found.some(n=>norm(leafText(n))===wantN)) break;
      }
      const matches=found.filter(n=>norm(leafText(n))===wantN);
      if(matches.length!==1) {
        const offered=found.map(leafText).slice(0,40);
        yield* closeList();
        return {...fail(matches.length ? 'ambiguous exact options' : 'no matching option in this control\'s own list'),
          noMatch:true, emptyRemote:offered.length===0, availableOptions:offered};
      }
      // The results list is virtualised: scrolling re-renders rows, so bring
      // the match into view first, then re-find it and click the live row.
      let target=matches[0];
      for(let i=0;i<6;i++) {
        target.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
        await sleep(200);
        const again=leaves().filter(n=>norm(leafText(n))===wantN);
        if(again.length!==1) continue;
        target=again[0];
        const r=target.getBoundingClientRect();
        const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
        if(top && (top===target || target.contains(top))) break;
      }
      const text=leafText(target);
      yield {node:target};
      await sleep(650);
      if(!valid()) return invalid();
      const picked=pills().find(t=>norm(t)===norm(text));
      yield* closeList();
      return picked ? {...out, ok:true, readback:picked, note:''} :
        {...out, ok:false, reverted:true, readback:pills().join(', '), note:'widget selection did not persist on the exact control'};
    }
    out.strategy='calendar-picker';
    let year, month=null, match;
    if ((match=String(want).match(/^(\d{4})$/))) year=+match[1];
    else if ((match=String(want).match(/^(\d{1,2})[\/-](\d{4})$/))) {month=+match[1];year=+match[2];}
    else if ((match=String(want).match(/^(\d{4})-(\d{1,2})$/))) {year=+match[1];month=+match[2];}
    else return fail('calendar expects yyyy or MM/yyyy');
    if(year<1900 || year>2200 || month!==null && (month<1||month>12)) return fail('calendar value outside supported bounds');
    // Local single-input container first; portalled calendars must be newly
    // opened by THIS gesture, not an unrelated pre-existing visible picker.
    const before=new Set([...document.querySelectorAll('.react-datepicker')].filter(visible));
    let scope=el.closest('.react-datepicker-wrapper');
    if(scope?.querySelectorAll('input').length!==1) scope=null;
    for(let i=0;scope && i<3 && !scope.matches('.form-group');i++) {
      if(scope.parentElement?.querySelectorAll('input').length!==1) break;
      scope=scope.parentElement;
    }
    yield {node:el};
    let cal=null;
    for(let i=0;i<16;i++) {
      await sleep(100);
      const linked=(el.getAttribute('aria-controls')||'').split(/\s+/).filter(Boolean)
        .map(id=>el.getRootNode().getElementById(id)).filter(n=>n?.matches('.react-datepicker')&&visible(n));
      const local=linked.length ? linked : scope && scope.querySelectorAll('input').length===1 ? [...scope.querySelectorAll('.react-datepicker')].filter(visible):[];
      const candidates=local.length ? local : [...document.querySelectorAll('.react-datepicker')].filter(n=>visible(n)&&!before.has(n));
      if(candidates.length===1) {cal=candidates[0];break;}
    }
    if(!cal) return fail('no uniquely scoped calendar opened for this input');
    const calendarHost=cal.closest('.react-datepicker-popper') || cal.parentElement;
    const isYear=!!cal.querySelector('.react-datepicker__year-text');
    const isMonth=!!cal.querySelector('.react-datepicker__month-text');
    if(!isYear&&!isMonth) return fail('unsupported calendar mode; no text was written');
    if(isYear && month!==null || isMonth && month===null) return fail('requested date precision does not match calendar capability');
    const monthNames=['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
    for(let step=0;step<128;step++) {
      if(!cal.isConnected && calendarHost?.isConnected) {
        const replacements=[...calendarHost.querySelectorAll('.react-datepicker')].filter(visible);
        if(replacements.length===1) cal=replacements[0];
      }
      if(!cal.isConnected || !el.isConnected) return fail('calendar detached before selection');
      if(isYear) {
        const cells=[...cal.querySelectorAll('.react-datepicker__year-text')];
        const pick=cells.find(n=>norm(n.textContent)===String(year));
        if(pick) {if(!enabled(pick)) return fail('requested year disabled');yield {node:pick};return await finish(()=>el.value,String(year));}
        const years=cells.map(n=>+n.textContent).filter(Number.isFinite);
        if(!years.length) return fail('no actual year options');
        const dir=year<Math.min(...years)?'previous':'next';
        const nav=cal.querySelector('.react-datepicker__navigation--'+dir);
        if(!enabled(nav)) return fail('requested year is outside calendar range');
        yield {node:nav};
      } else {
        const yearSelect=[...cal.querySelectorAll('select')].find(n=>[...n.options].every(o=>/^\d{4}$/.test(o.text)));
        const shown=yearSelect ? +yearSelect.value : +(cal.querySelector('.react-datepicker__header, .react-datepicker-year-header')?.textContent.match(/\b\d{4}\b/)||[])[0];
        if(!shown) return fail('cannot determine displayed calendar year');
        if(shown!==year) {
          if(yearSelect) {
            const opts=[...yearSelect.options];const idx=opts.findIndex(o=>+o.value===year&&!o.disabled);
            if(idx<0) return fail('requested year not offered in calendar');
            yield {node:yearSelect,key:idx<yearSelect.selectedIndex?'ArrowUp':'ArrowDown'};
          } else {
            const nav=cal.querySelector('.react-datepicker__navigation--'+(year<shown?'previous':'next'));
            if(!enabled(nav)) return fail('requested year outside calendar navigation');
            yield {node:nav};
          }
        } else {
          const cells=[...cal.querySelectorAll('.react-datepicker__month-text')];
          const pick=cells.find(n=>norm(n.textContent).slice(0,3)===monthNames[month-1]);
          if(!enabled(pick)) return fail('requested month not offered or disabled');
          yield {node:pick};
          // Accept the format actually displayed, but only the same year/month.
          const result=await finish(()=>{
            const v=el.value;const a=v.match(/^(\d{1,2})[\/-](\d{4})$/), b=v.match(/^(\d{4})-(\d{1,2})$/);
            return a ? +a[2]+'-'+ +a[1] : b ? +b[1]+'-'+ +b[2] : v;
          },year+'-'+month);
          result.readback=el.isConnected ? el.value : '';
          return result;
        }
      }
      await sleep(90);
    }
    return fail('calendar navigation limit reached; no value injected');
  }
  const iterator=run();
  return {guard, next:async()=>{
    if(!valid()) return {done:true,result:invalid()};
    const step=await iterator.next();
    if(!valid()) return {done:true,result:invalid()};
    if(step.done) return {done:true,result:step.value};
    const {node,key,check,text,clear,noEnter}=step.value;
    const identity=nodeIdentity(node);
    if(!enabled(node)||!node.isConnected) return {done:true,result:fail('gesture target detached or disabled')};
    const surfaces=[node,...(check?[...(node.labels||[])].filter(l=>l.control===node):[]),
      ...(check && node===el && wrapper ? [wrapper] : [])];
    for(const s of surfaces) {
      if(!visible(s)) continue;
      // A listbox that just opened may still run its own "scroll to the
      // current value" (Workday does, ~100ms after opening), undoing ours.
      // Re-scroll until the target is really inside the viewport.
      // Also scroll INSIDE the nearest scrolling list: an option can sit in
      // the viewport yet be clipped by its own listbox (the last option of a
      // 250-country list hit the label underneath instead).
      const scroller=(()=>{for(let n=s.parentElement;n&&n!==document.body;n=n.parentElement){
        const cs=getComputedStyle(n);if(/(auto|scroll)/.test(cs.overflowY)&&n.scrollHeight>n.clientHeight+1)return n;}return null;})();
      const shown=()=>{const rr=s.getBoundingClientRect();
        if(!(rr.top>=0&&rr.bottom<=innerHeight&&rr.left>=0&&rr.right<=innerWidth))return false;
        if(!scroller)return true;const sr=scroller.getBoundingClientRect();
        return rr.top>=sr.top-1&&rr.bottom<=sr.bottom+1;};
      for(let tries=0;tries<8;tries++) {
        if(scroller){const sr=scroller.getBoundingClientRect(),rr=s.getBoundingClientRect();
          scroller.scrollTop+= (rr.top+rr.height/2)-(sr.top+sr.height/2);}
        // Centre in the window: 'nearest' parks the control at the bottom
        // edge, under sticky footers (Workday's Cancel/Submit bar).
        s.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
        if(scroller){const sr=scroller.getBoundingClientRect(),rr=s.getBoundingClientRect();
          scroller.scrollTop+= (rr.top+rr.height/2)-(sr.top+sr.height/2);}
        await sleep(tries ? 150 : 90);
        if(shown()) break;
      }
      if(!valid()||!ownsSurface(node,s)||!node.isConnected||!nodeIdentity(node).every((v,i)=>v===identity[i])) return {done:true,result:invalid()};
      const r=s.getBoundingClientRect();
      for(const [fx,fy] of [[.5,.5],[.15,.5],[.85,.5]]) {
        const x=r.left+r.width*fx,y=r.top+r.height*fy;
        let top=document.elementFromPoint(x,y);
        while(top?.shadowRoot) {const deep=top.shadowRoot.elementFromPoint(x,y);if(!deep||deep===top)break;top=deep;}
        if(!hitOwns(s,top)) continue;
        if(check&&!ownsHit(node,top)) continue;
        if(key) node.focus();
        pending={node,s,x,y,key,check,identity};
        if(!guard()) return {done:true,result:invalid()};
        return {done:false,x,y,key:key||null,text:text||null,clear:!!clear,noEnter:!!noEnter};
      }
    }
    return {done:true,result:fail('exact widget is obscured or has no safe hit-test point')};
  }};
})
"""


def drive_widget(cdp, token, data):
    """Use the existing page connection; never resolve another tab or label."""
    slot = 'window[' + json.dumps(token) + ']'
    obj = None
    old_deadline = getattr(cdp, 'deadline', None)
    cdp.deadline = time.monotonic() + WIDGET_TIMEOUT_SEC
    def call(method, params):
        if time.monotonic() >= cdp.deadline:
            raise TimeoutError('widget operation deadline exceeded')
        reply = cdp.call(method, params)
        if reply.get('error') or reply.get('result', {}).get('exceptionDetails'):
            raise RuntimeError('widget CDP operation failed')
        return reply
    def guarded():
        reply = call('Runtime.callFunctionOn', {'objectId':obj, 'functionDeclaration':'function(){return this.guard()}', 'returnByValue':True})
        return reply.get('result', {}).get('result', {}).get('value') is True
    def best_effort(method, params):
        try:
            cdp.call(method, params)
        except Exception:
            pass
    def release_input(method, params):
        # A timed-out down event still needs an up; reserve a bounded cleanup
        # window rather than letting the expired operation budget suppress it.
        deadline = cdp.deadline
        cdp.deadline = max(deadline, time.monotonic() + .5)
        try:
            best_effort(method, params)
        finally:
            cdp.deadline = deadline
    try:
        reply = call('Runtime.evaluate', {'expression': f'({WIDGET_JS})({slot})'})
        obj = reply.get('result', {}).get('result', {}).get('objectId')
        if not obj:
            return {**data, 'ok': False, 'note': 'could not initialize exact widget handler'}
        for _ in range(140):
            reply = call('Runtime.callFunctionOn', {'objectId': obj, 'functionDeclaration': 'function(){return this.next()}', 'awaitPromise': True, 'returnByValue': True})
            step = reply.get('result', {}).get('result', {}).get('value')
            if not step:
                return {**data, 'ok': False, 'note': 'widget handler failed; no success inferred'}
            if step['done']:
                return step['result']
            if not guarded():
                return {**data, 'ok':False, 'note':'exact widget changed after hit-test; refused input'}
            if step.get('key') == 'type':
                # Trusted text entry into the focused search box, then Enter
                # (Workday prompts search on Enter).
                if step.get('clear'):
                    sel_all = {'key': 'a', 'code': 'KeyA', 'windowsVirtualKeyCode': 65, 'modifiers': 2}
                    call('Input.dispatchKeyEvent', {**sel_all, 'type': 'keyDown'})
                    release_input('Input.dispatchKeyEvent', {**sel_all, 'type': 'keyUp'})
                    bksp = {'key': 'Backspace', 'code': 'Backspace', 'windowsVirtualKeyCode': 8}
                    call('Input.dispatchKeyEvent', {**bksp, 'type': 'keyDown'})
                    release_input('Input.dispatchKeyEvent', {**bksp, 'type': 'keyUp'})
                if step.get('text'):
                    call('Input.insertText', {'text': step['text']})
                if not step.get('noEnter'):
                    enter = {'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13}
                    try:
                        call('Input.dispatchKeyEvent', {**enter, 'type': 'keyDown', 'text': '\r'})
                    finally:
                        release_input('Input.dispatchKeyEvent', {**enter, 'type': 'keyUp'})
            elif step.get('key'):
                key = step['key']
                code = {'ArrowUp': 38, 'ArrowDown': 40, 'Tab': 9, 'Escape': 27}.get(key, 40)
                params={'key':key,'code':key,'windowsVirtualKeyCode':code}
                try:
                    call('Input.dispatchKeyEvent', {**params,'type':'keyDown'})
                finally:
                    # Release even if keyDown or focus validation fails.
                    try:
                        guarded()
                    finally:
                        release_input('Input.dispatchKeyEvent', {**params,'type':'keyUp'})
            else:
                params={'x':step['x'],'y':step['y'],'button':'left','clickCount':1}
                try:
                    call('Input.dispatchMouseEvent', {**params,'type':'mousePressed'})
                finally:
                    safe = False
                    try:
                        safe = guarded()
                    finally:
                        # A replacement must not receive the completing mouse-up.
                        release=params if safe else {**params,'x':-1,'y':-1,'clickCount':0}
                        release_input('Input.dispatchMouseEvent', {**release,'type':'mouseReleased'})
        return {**data, 'ok': False, 'note':'widget gesture limit reached'}
    except Exception as exc:
        return {**data, 'ok': False, 'note':str(exc)}
    finally:
        # Independent bounded cleanup; a failed deletion cannot skip release.
        cdp.deadline = time.monotonic() + .5
        best_effort('Runtime.evaluate', {'expression':f'delete {slot}'})
        if obj:
            cdp.deadline = time.monotonic() + .5
            best_effort('Runtime.releaseObject', {'objectId':obj})
        cdp.deadline = old_deadline
