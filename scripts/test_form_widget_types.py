#!/usr/bin/env python3
"""Self-contained trusted-UI widget tests. Never connects to production CDP.
uv run python scripts/test_form_widget_types.py [--vendor /tmp/renderer.js] [--legacy]
The optional vendor bundle is mounted locally; no network or real application.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from module4 import form_filler as f
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vendor', type=Path)
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--wrappers-only', action='store_true')
    args = parser.parse_args()
    results = []
    def check(name, ok, detail=''):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f' {detail}' if detail else ''), flush=True)
    with tempfile.TemporaryDirectory(prefix='jobo-widget-', ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(['/usr/bin/chromium', '--headless', '--no-sandbox', '--disable-dev-shm-usage', '--remote-debugging-port=0', '--user-data-dir='+profile, 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = Path(profile)/'DevToolsActivePort'
            for _ in range(100):
                if portfile.exists(): break
                time.sleep(.05)
            cdp = 'http://127.0.0.1:'+portfile.read_text().splitlines()[0]
            assert not cdp.endswith(':9333')
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp)
                page = browser.contexts[0].pages[0]
                page.goto(Path(__file__).with_name('form_widget_probe.html').as_uri())
                def listing():
                    ls = f.list_fields(cdp); f.remember_listing(cdp, ls); return ls
                def field(id):
                    return next((x for x in listing().fields if x.sig.split('~')[1] == id), None)
                def setval(id, value):
                    fi = field(id)
                    return f.set_field(cdp, '#'+str(fi.index), value, index=fi.index) if fi else f.SetFieldResult(False, note='not listed')
                original_call=f._Cdp.call
                for mode in ['unchanged', 'checkbox', 'no aria', 'two choices', 'wrong role', 'contradictory', 'already contradictory'] + [stage+' '+drift for stage in ['wait','yield','press','final'] for drift in ['retarget','remove','role','data-value','identity']]:
                    kind='checkbox' if mode=='checkbox' else 'radio'
                    page.set_content('<div id="surface" role="'+kind+'" data-value="No" aria-checked="false" style="padding:20px;width:200px"><div><div><input style="display:none" type="'+kind+'" name="answer" value="No" aria-labelledby="choice-text"></div></div><div id="choice-text"><div><p>No</p></div></div></div><input id="sibling" style="display:none" type="radio" value="Other" aria-label="Other">')
                    page.evaluate('''mode=>{
                        window.controlEvents=[];window.drifted=false;
                        const s=document.querySelector('#surface'), e=s.querySelector('input');
                        window.requested=e;
                        s.onclick=ev=>{controlEvents.push(ev.isTrusted);const target=s.querySelector('input');if(target)target.checked=true;if(!['contradictory','no aria'].includes(mode))s.setAttribute('aria-checked','true');if(mode.startsWith('final '))setTimeout(drift,20)};
                        window.drift=()=>{drifted=true;const change=mode.split(' ')[1];
                            if(change==='retarget'){e.remove();s.prepend(document.querySelector('#sibling'))}
                            if(change==='remove')s.replaceWith(...s.childNodes);
                            if(change==='role')s.setAttribute('role','checkbox');
                            if(change==='data-value')s.setAttribute('data-value','Other');
                            if(change==='identity')e.value='Other';
                        };
                        if(mode.startsWith('wait '))s.scrollIntoView=()=>setTimeout(drift,20);
                        if(mode==='two choices')s.prepend(document.querySelector('#sibling'));
                        if(mode==='wrong role')s.setAttribute('role','checkbox');
                        if(mode==='no aria')s.removeAttribute('aria-checked');
                        if(mode==='already contradictory')e.checked=true;
                    }''',mode)
                    drifted=[False]
                    def wrapper_call(self, method, params=None):
                        reply=original_call(self,method,params)
                        value=reply.get('result',{}).get('result',{}).get('value',{})
                        yielded=method=='Runtime.callFunctionOn' and isinstance(value,dict) and 'x' in value
                        pressed=method=='Input.dispatchMouseEvent' and (params or {}).get('type')=='mousePressed'
                        if not drifted[0] and ((mode.startswith('yield ') and yielded) or (mode.startswith('press ') and pressed)):
                            drifted[0]=True
                            original_call(self,'Runtime.evaluate',{'expression':'drift()'})
                        return reply
                    fi=next(x for x in listing().fields if x.label=='No')
                    f._Cdp.call=wrapper_call
                    try:r=f.set_field(cdp,'#'+str(fi.index),'No',index=fi.index)
                    finally:f._Cdp.call=original_call
                    state=page.evaluate('({checked:requested.checked,sibling:document.querySelector("#sibling").checked,events:controlEvents,drifted})')
                    good=mode in ['unchanged','checkbox','no aria']
                    safe=(r.ok and state['checked'] and state['events']==[True]) if good else (not r.ok)
                    if not good and mode not in ['contradictory','already contradictory'] and not mode.startswith('final '):
                        safe=safe and not state['checked'] and state['events']==[]
                    if mode=='contradictory' or mode.startswith('final '):
                        safe=safe and state['checked'] and state['events']==[True]
                    if mode=='already contradictory':safe=safe and state['checked'] and state['events']==[]
                    if mode.split(' ')[0] in ['wait','yield','press','final']:safe=safe and state['drifted']
                    check('Rippling wrapper '+mode,safe and not state['sibling'],json.dumps(state)+' '+r.note)
                    if good:
                        fi=next(x for x in listing().fields if x.label=='No')
                        r=f.set_field(cdp,'#'+str(fi.index),'No',index=fi.index)
                        check('Rippling wrapper idempotent '+mode,r.ok and page.evaluate('controlEvents.length')==1,r.note)
                if args.wrappers_only:
                    browser.close()
                    print(f'{sum(results)}/{len(results)} widget checks passed',flush=True)
                    return 0 if all(results) else 1
                page.goto(Path(__file__).with_name('form_widget_probe.html').as_uri())
                fi = field('worker-no')
                check('radio listing exposes checked=false', fi.kind == 'radio' and getattr(fi, 'checked', None) is False and 'checked=false' in fi.as_line())
                r = setval('worker-no','No')
                check('radio selects exact No with trusted click', r.ok and page.locator('#worker-no').is_checked() and not page.locator('#unrelated-no').is_checked(), r.note)
                count = page.evaluate('events.length');r = setval('worker-no','No')
                check('radio already checked is idempotent',r.ok and page.evaluate('events.length')==count)
                r = setval('worker-no','false')
                check('checked radio false fails without selecting sibling',not r.ok and page.locator('#worker-no').is_checked() and not page.locator('#worker-yes').is_checked())
                r = setval('worker-yes','false')
                check('unchecked radio false is idempotent',r.ok and not page.locator('#worker-yes').is_checked())
                for want in ['true','false']:
                    r = setval('consent',want)
                    check('checkbox '+want+' verifies checked and preserves value',r.ok and page.locator('#consent').is_checked()==(want=='true') and page.locator('#consent').input_value()=='consent-token')
                r = setval('consent','maybe');check('invalid checkbox state refused',not r.ok and page.locator('#consent').input_value()=='consent-token')
                r = setval('reverting','true');check('reverted checkbox never reports success',not r.ok and not page.locator('#reverting').is_checked())
                for id,want in [('year-from','1999'),('year-to','2022'),('month','11/2022')]:
                    r=setval(id,want);check('calendar '+id+' commits via UI',r.ok and page.evaluate('(id)=>held[id]',id)==want,r.note)
                check('two year inputs remain independent',page.locator('#year-from').input_value()=='1999' and page.locator('#year-to').input_value()=='2022')
                for id,want,expected in [('plain','2022','2022'),('native-date','10/04/2026','2026-10-04')]:
                    r=setval(id,want);check('compatible '+id,r.ok and page.locator('#'+id).input_value()==expected)
                fi=field('field-67');check('bare combo numbered with question and required',bool(fi and fi.kind=='combobox' and 'legally authorized' in fi.label and fi.required))
                check('wrapped combo logically deduplicated',len([x for x in listing().fields if x.sig.split('~')[1]=='wrapped'])==1)
                page.evaluate('window.scrollTo(0,0)');r=setval('field-67','Yes')
                check('offscreen bare combo opens own delayed options and commits',r.ok and page.evaluate('held["field-67"]')=='Yes',r.note)
                r=setval('field-67','Atlantis');check('bare combo never invents option',not r.ok and r.no_match and r.available_options==['Yes','No'] and page.evaluate('held["field-67"]')=='Yes')
                r=setval('empty','Yes');check('empty own list ignores decoy options',not r.ok and r.no_match and r.empty_remote and page.locator('#empty p').inner_text()=='Select')
                check('radio option values are never overwritten',page.locator('#worker-no').input_value()=='No' and page.locator('#worker-yes').input_value()=='Yes')
                r=setval('field-67','');check('empty menu query never selects anything',not r.ok and page.evaluate('held["field-67"]')=='Yes')
                page.locator('#consent').evaluate('(e)=>e.disabled=true');r=setval('consent','true')
                check('disabled checkbox refuses gesture',not r.ok and not page.locator('#consent').is_checked())
                page.locator('#consent').evaluate('(e)=>e.disabled=false')
                r=setval('month','2022');check('month picker rejects year-only precision',not r.ok and page.locator('#month').input_value()=='11/2022')
                r=setval('year-from','1800');check('calendar navigation bounds fail closed',not r.ok and page.locator('#year-from').input_value()=='1999')
                # Occlusion is not a reason to remove overlays or dispatch DOM clicks.
                page.locator('#consent').evaluate('(e)=>{e.scrollIntoView();const d=document.createElement("div");d.id="cover";d.style="position:fixed;inset:0;z-index:99999;background:white";document.body.append(d)}')
                r=setval('consent','true');check('occluded checkbox fails without toggling',not r.ok and not page.locator('#consent').is_checked())
                page.locator('#cover').evaluate('(e)=>e.remove()')
                fi=field('consent');page.locator('#consent').evaluate('(e)=>e.id="replacement"')
                r=f.set_field(cdp,'#'+str(fi.index),'true',index=fi.index);check('stale signature still refuses',not r.ok and r.stale)
                check('all fixture selection events trusted',page.evaluate('events.every(e=>e[1])'))
                # GHO-23: a trusted click that never reaches the page (Avature consent
                # boxes on the form workers) gets the box's own click(), once.
                page.locator('#replacement').evaluate('(e)=>{e.id="consent";e.checked=false;e.scrollIntoView()}')
                ign=page.context.new_cdp_session(page);ign.send('Input.setIgnoreInputEvents',{'ignore':True})
                try: r=setval('consent','true')
                finally: ign.send('Input.setIgnoreInputEvents',{'ignore':False});ign.detach()
                check('checkbox whose trusted click never arrives: own click() once, verified',
                      r.ok and r.strategy=='trusted-checkbox+dom-click' and page.locator('#consent').is_checked(),r.strategy+' '+r.note)
                r=setval('reverting','true')
                check('reverted checkbox (click arrived) gets no DOM click',not r.ok and 'dom-click' not in r.strategy,r.strategy)
                # Review regressions: each case starts from an isolated minimal DOM.
                for id, attrs in [('aria-no', 'aria-label="No"'), ('labelled-no', 'aria-labelledby="no-label"')]:
                    page.set_content('<span id="no-label" hidden>No</span><div>Unrelated question<input id="'+id+'" type="radio" name="answer" value="n" '+attrs+'></div>')
                    fi=field(id); r=setval(id,'No')
                    check('accessible radio label '+id,bool(fi and fi.label=='No') and r.ok and page.locator('#'+id).is_checked() and page.locator('#'+id).input_value()=='n',r.note)
                page.set_content('<label>No<input id="mutating" type="radio" name="answer" value="No"></label>')
                page.evaluate('''()=>{const e=document.querySelector('input');e.scrollIntoView=()=>setTimeout(()=>{e.value='Yes';e.name='other'},20)}''')
                r=setval('mutating','No')
                check('radio identity revalidated after scroll wait',not r.ok and not page.locator('#mutating').is_checked(),r.note)
                page.set_content('<label>No<input id="final-mutating" type="radio" name="answer" value="No"></label>')
                page.evaluate('''()=>{const e=document.querySelector('input');e.onclick=()=>setTimeout(()=>e.name='other',100)}''')
                r=setval('final-mutating','No')
                check('radio identity revalidated at final readback',not r.ok,r.note)
                page.set_content('<label>Consent<input id="replace-me" type="checkbox" value="token"></label>')
                original_call=f._Cdp.call
                replaced=[False]
                def replacing_call(self, method, params=None):
                    reply=original_call(self,method,params)
                    value=reply.get('result',{}).get('result',{}).get('value',{})
                    if method=='Runtime.callFunctionOn' and isinstance(value,dict) and 'x' in value and not replaced[0]:
                        replaced[0]=True
                        original_call(self,'Runtime.evaluate',{'expression':"document.querySelector('input').replaceWith(document.querySelector('input').cloneNode(true))"})
                    return reply
                f._Cdp.call=replacing_call
                try:r=setval('replace-me','true')
                finally:f._Cdp.call=original_call
                check('replacement after hit-test never clicked',not r.ok and not page.locator('#replace-me').is_checked(),r.note)
                # Hidden controls must keep ownership of their visible label until input.
                for mode in ['unchanged', 'scroll wait', 'after hit-test', 'mouse press', 'exposed label']:
                    page.set_content('''<input id="requested" type="checkbox" aria-label="Consent" hidden><input id="sibling" type="checkbox" aria-label="Sibling" hidden><label id="surface" for="requested" style="display:inline-block;padding:20px"><span>Consent</span></label>''')
                    page.evaluate('''mode=>{
                        window.controlEvents=[];window.retargeted=false;
                        for(const type of ['click','change']) document.addEventListener(type,e=>{
                            if(e.target.matches('#requested,#sibling')) controlEvents.push([e.target.id,type,e.isTrusted]);
                        },true);
                        window.retarget=()=>{
                            retargeted=true;
                            if(mode==='exposed label') document.querySelector('#surface').innerHTML='<label for="sibling">Consent</label>';
                            else document.querySelector('#surface').htmlFor='sibling';
                        };
                        if(mode==='scroll wait') document.querySelector('#surface').scrollIntoView=()=>setTimeout(retarget,20);
                    }''',mode)
                    retargeted=[False]
                    def retargeting_call(self, method, params=None):
                        reply=original_call(self,method,params)
                        value=reply.get('result',{}).get('result',{}).get('value',{})
                        yielded=method=='Runtime.callFunctionOn' and isinstance(value,dict) and 'x' in value
                        pressed=method=='Input.dispatchMouseEvent' and (params or {}).get('type')=='mousePressed'
                        if not retargeted[0] and ((mode in ['after hit-test','exposed label'] and yielded) or (mode=='mouse press' and pressed)):
                            retargeted[0]=True
                            original_call(self,'Runtime.evaluate',{'expression':'retarget()'})
                        return reply
                    f._Cdp.call=retargeting_call
                    try:r=setval('requested','true')
                    finally:f._Cdp.call=original_call
                    state=page.evaluate('({events:controlEvents,retargeted,requested:document.querySelector("#requested").checked,sibling:document.querySelector("#sibling").checked})')
                    safe=(r.ok and state['requested'] and not state['sibling'] and state['events']==[['requested','click',True],['requested','change',True]]) if mode=='unchanged' else (not r.ok and state['retargeted'] and not state['requested'] and not state['sibling'] and state['events']==[])
                    check('hidden checkbox label ownership '+mode,safe,json.dumps(state)+' '+r.note)
                page.set_content('<input id="hidden-no" type="radio" name="answer" value="n" aria-label="No" hidden><label for="hidden-no">No</label>')
                r=setval('hidden-no','No')
                check('hidden radio label selects exact No',r.ok and page.locator('#hidden-no').is_checked() and page.locator('#hidden-no').input_value()=='n',r.note)
                page.set_content('''<div class="form-group"><div class="react-datepicker-wrapper"><input id="first"></div><div class="react-datepicker-wrapper"><input id="second"></div><div class="react-datepicker"><div class="react-datepicker__year-text" onclick="document.querySelector('#second').value='2022'">2022</div></div></div>''')
                r=setval('first','2022')
                check('preexisting sibling calendar never used',not r.ok and page.locator('#second').input_value()=='',r.note)
                for selected in ['false', None]:
                    attr='' if selected is None else ' aria-selected="false"'
                    page.set_content('<label for="uncommitted">Answer</label><div id="uncommitted" role="combobox" tabindex="0" aria-controls="own">Yes</div><ul id="own" role="listbox"><li role="option"'+attr+'>Yes</li></ul>')
                    r=setval('uncommitted','Yes')
                    check('unchanged display is not committed '+str(selected),not r.ok,r.note)
                page.set_content('''<label for="commit">Answer</label><div id="commit" role="combobox" tabindex="0" aria-controls="own">Select</div><ul id="own" role="listbox"><li role="option" aria-selected="false" onclick="document.querySelector('#commit').textContent='Yes'">Yes</li></ul>''')
                r=setval('commit','Yes')
                check('authoritative false overrides changed display',not r.ok,r.note)
                # Shine.com job-apply-flow select-field (job1455): no aria-controls,
                # the opened <ul role=listbox> has no id and mounts inside the
                # control's own wrapper; a trusted option click commits and unmounts it.
                page.set_content('''<style>.select-options-list{max-height:180px;overflow:auto;margin:0;padding:0}</style>
                  <div class="rc-exp-selects"><div class="rc-field--exp" id="years-box"></div><div class="rc-field--exp" id="months-box"></div></div>
                  <div class="rc-field" id="salary-box"></div>
                  <div id="shared-box"></div>''')
                page.evaluate('''()=>{
                  window.held={};window.events=[];
                  const yrs=[...Array(31).keys()].map(i=>i+(i===1?' year':' years'));
                  const mos=[...Array(12).keys()].map(i=>i+(i===1?' month':' months'));
                  const lakhs=[...Array(51).keys()].map(i=>i+' Lakhs');
                  const build=(box,id,label,shown,opts)=>{
                    const w=document.createElement('div');w.className='select-wrapper select-md';
                    w.innerHTML=(label?'<label for="'+id+'" class="select-label">'+label+'</label>':'')+
                      '<div id="'+id+'" class="select-field select-md" tabindex="0" role="combobox" aria-expanded="false" aria-haspopup="listbox" aria-label="'+(label||'')+'">'+
                      '<div class="select-content"><div class="select-field-input">'+shown+'</div><button class="select-dropdown-button" tabindex="-1" type="button"><svg width="12" height="12"></svg></button></div></div>';
                    box.append(w);
                    const el=w.querySelector('[role=combobox]'), shownNode=w.querySelector('.select-field-input');
                    const close=()=>{w.querySelector('.select-dropdown')?.remove();el.setAttribute('aria-expanded','false')};
                    el.addEventListener('click',ev=>{
                      if(!ev.isTrusted)return;
                      if(w.querySelector('.select-dropdown')){close();return;}
                      setTimeout(()=>{
                        const d=document.createElement('div');d.className='select-desktop-dropdown select-dropdown';
                        const ul=document.createElement('ul');ul.className='select-options-list';ul.setAttribute('role','listbox');ul.setAttribute('aria-label','Options');
                        for(const text of opts){
                          const li=document.createElement('li');li.className='select-option';li.setAttribute('role','option');
                          li.setAttribute('aria-selected',String(held[id]===text));
                          li.innerHTML='<div class="select-option-content"><div class="select-option-text"><div class="select-option-label">'+text+'</div></div></div>';
                          li.addEventListener('click',e=>{e.stopPropagation();if(!e.isTrusted)return;events.push([id,text]);held[id]=text;shownNode.textContent=text;close()});
                          ul.append(li);
                        }
                        d.append(ul);w.append(d);el.setAttribute('aria-expanded','true');
                      },120);
                    });
                    return w;
                  };
                  build(document.getElementById('years-box'),'_r_8_-select','','0 years',yrs);
                  build(document.getElementById('months-box'),'_r_a_-select','','0 months',mos);
                  build(document.getElementById('salary-box'),'_r_c_-select-current-salary','Current Salary','Select Current Salary',lakhs);
                  // Two controls in one wrapper: an unlinked list there is ambiguous.
                  const other=document.createElement('div');other.id='shared-b';other.tabIndex=0;other.setAttribute('role','combobox');other.textContent='Other';
                  build(document.getElementById('shared-box'),'shared-a','Shared A','Select',['Yes','No']).append(other);
                }''')
                r=setval('_r_c_-select-current-salary','7 Lakhs')
                fi=field('_r_c_-select-current-salary')
                check('shine unlinked salary list commits and reads back',r.ok and r.readback=='7 Lakhs' and page.evaluate('held["_r_c_-select-current-salary"]')=='7 Lakhs' and bool(fi and fi.value=='7 Lakhs'),r.note+' '+str(fi and fi.value))
                check('shine salary list closed after commit',page.evaluate('document.querySelectorAll("[role=listbox]").length')==0)
                r=setval('_r_8_-select','1 year');r2=setval('_r_a_-select','6 months')
                check('shine sibling experience selects commit independently',r.ok and r2.ok and page.evaluate('[held["_r_8_-select"],held["_r_a_-select"],held["_r_c_-select-current-salary"]]')==['1 year','6 months','7 Lakhs'],r.note+' '+r2.note)
                r=setval('_r_c_-select-current-salary','20 LPA')
                check('shine wrong salary text lists real options, keeps value',not r.ok and r.no_match and '7 Lakhs' in r.available_options and page.evaluate('held["_r_c_-select-current-salary"]')=='7 Lakhs' and page.evaluate('document.querySelectorAll("[role=listbox]").length')==0,r.note)
                r=setval('shared-a','Yes')
                check('unlinked list beside another combobox is refused',not r.ok and 'shared-a' not in page.evaluate('held'),r.note)
                check('shine fixture option clicks all trusted and exact',page.evaluate('events')==[['_r_c_-select-current-salary','7 Lakhs'],['_r_8_-select','1 year'],['_r_a_-select','6 months']])
                # Keyboard routing is guarded between JS yielding and CDP input.
                page.set_content('''<label>Other<input id="other"></label><div class="form-group"><div class="react-datepicker-wrapper"><input id="keyboard"></div><div class="react-datepicker"><select><option>2025</option><option selected>2026</option></select><div class="react-datepicker__month-text">Jan</div></div></div>''')
                page.evaluate("()=>{window.wrongKeys=0;document.querySelector('#other').onkeydown=()=>wrongKeys++}")
                def focus_stealing_call(self, method, params=None):
                    reply=original_call(self,method,params)
                    value=reply.get('result',{}).get('result',{}).get('value',{})
                    if method=='Runtime.callFunctionOn' and isinstance(value,dict) and value.get('key'):
                        original_call(self,'Runtime.evaluate',{'expression':"document.querySelector('#other').focus()"})
                    return reply
                f._Cdp.call=focus_stealing_call
                try:r=setval('keyboard','01/2025')
                finally:f._Cdp.call=original_call
                check('keyboard focus theft refuses trusted keyDown',not r.ok and page.evaluate('wrongKeys')==0,r.note)
                from module4 import form_widgets as widgets
                class BrokenCdp:
                    def __init__(self):self.calls=[]
                    def call(self,method,params):
                        self.calls.append((method,params))
                        if method=='Runtime.evaluate':
                            if params['expression'].startswith('delete'):raise RuntimeError('deletion failed')
                            return {'result':{'result':{'objectId':'object'}}}
                        if method=='Runtime.callFunctionOn':
                            value=True if 'guard' in params['functionDeclaration'] else {'done':False,'x':1,'y':1,'key':'ArrowDown'}
                            return {'result':{'result':{'value':value}}}
                        if method=='Input.dispatchKeyEvent' and params['type']=='keyDown':raise RuntimeError('keyDown failed')
                        return {}
                broken=BrokenCdp(); verdict=widgets.drive_widget(broken,'test',{})
                check('keyUp finally and release survive independent failures',not verdict['ok'] and any(m=='Input.dispatchKeyEvent' and p['type']=='keyUp' for m,p in broken.calls) and any(m=='Runtime.releaseObject' for m,p in broken.calls))
                class FloodSocket:
                    def __init__(self):self.timeouts=[]
                    def settimeout(self,t):self.timeouts.append(t)
                    def send(self,msg):pass
                    def recv(self):time.sleep(.002);return '{"method":"unrelated.event"}'
                connection=f._Cdp.__new__(f._Cdp);connection.ws=FloodSocket();connection._id=0;connection.deadline=time.monotonic()+.025
                started=time.monotonic()
                try:connection.call('Runtime.evaluate',{});bounded=False
                except TimeoutError:bounded=time.monotonic()-started<.25
                check('CDP receive event flood obeys whole-operation deadline',bounded and max(connection.ws.timeouts)<=.025)
                if args.vendor:
                    bundle=args.vendor.read_text().replace('},{},["nKGo"], null)','},{},[], null)')
                    for fmt,values in [('yyyy',['1999','2022']),('MM/yyyy',['11/2022','12/2023'])]:
                        page.set_content('<div id="root"></div>');page.add_script_tag(content=bundle)
                        page.evaluate('''fmt=>{const R=parcelRequire('HdMw'),D=parcelRequire('X9zx'),C=parcelRequire('ZoEN').default;window.held={};D.render(R.createElement(C,{schema:{format:fmt,HtmlInputName1:'startDate',HtmlInputName2:'endDate'},uiSchema:{isMasked:false,canEdit:false},errorSchema:{},idSchema:{startDate:{$id:'startDate'},endDate:{$id:'endDate'}},formData:{},name:'fromTo',onChange:d=>window.held=d}),document.getElementById('root'))}''',fmt)
                        for id,want in zip(['startDate','endDate'],values):
                            r=setval(id,want);check('actual Phenom '+fmt+' '+id,r.ok and page.evaluate('(id)=>held[id]',id)==want,r.note)
                            if not r.ok: print('VENDOR_DOM',page.locator('#root').inner_html()[:14000],flush=True)
                if args.legacy:
                    spec=importlib.util.spec_from_file_location('legacy',Path(__file__).with_name('test_form_filler.py'));legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
                    legacy.CDP=cdp
                    # Keep isolated guard page; no browser_session production recovery.
                    legacy.close_other_tabs=lambda keep: None
                    check('existing form filler suite',legacy.main()==0)
                browser.close()
        finally:
            proc.terminate();proc.wait(timeout=10)
    print(f'{sum(results)}/{len(results)} widget checks passed',flush=True)
    return 0 if all(results) else 1

if __name__=='__main__':raise SystemExit(main())
