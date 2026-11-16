/* Rebalance motion system. No dependencies; disabled for reduced motion. */
(() => {
  'use strict';
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const home = document.getElementById('v-home');
  if (!home) return;

  document.body.classList.add('motion-ready');

  const progress = document.createElement('div');
  progress.className = 'rb-progress';
  progress.setAttribute('aria-hidden', 'true');
  document.body.appendChild(progress);

  const loader = document.createElement('div');
  loader.className = 'rb-loader';
  loader.setAttribute('aria-hidden', 'true');
  loader.innerHTML = '<div class="rb-loader-mark"></div>';
  document.body.appendChild(loader);
  requestAnimationFrame(() => setTimeout(() => loader.classList.add('done'), 520));
  setTimeout(() => loader.remove(), 1900);

  if (reduce) return;

  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const revealObserver = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('in');
      revealObserver.unobserve(entry.target);
    });
  }, { threshold: .16, rootMargin: '0px 0px -7% 0px' });

  home.querySelectorAll('.lead2 > *, .prow, .g3 .c, .contact .crow, .ctaband > *').forEach((el, i) => {
    el.classList.add('stagger-child');
    el.style.setProperty('--i', String(i % 5));
  });

  home.querySelectorAll('.lead2 h2, .approach h2, .visit-card h2, .ctaband h2').forEach(heading => {
    const walker = document.createTreeWalker(heading, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) if (walker.currentNode.nodeValue.trim()) nodes.push(walker.currentNode);
    let index = 0;
    nodes.forEach(node => {
      const fragment = document.createDocumentFragment();
      node.nodeValue.split(/(\s+)/).forEach(part => {
        if (!part.trim()) return fragment.appendChild(document.createTextNode(part));
        const outer = document.createElement('span');
        outer.className = 'word-rise';
        outer.style.setProperty('--i', index++);
        const inner = document.createElement('span');
        inner.textContent = part;
        outer.appendChild(inner);
        fragment.appendChild(outer);
        fragment.appendChild(document.createTextNode(' '));
        revealObserver.observe(outer);
      });
      node.replaceWith(fragment);
    });
  });

  home.querySelectorAll('.reveal, .stagger-child').forEach(el => revealObserver.observe(el));

  let ticking = false;
  const paintScroll = () => {
    const max = document.documentElement.scrollHeight - innerHeight;
    progress.style.transform = `scaleX(${max > 0 ? scrollY / max : 0})`;
    const visual = home.querySelector('.hero-visual');
    if (visual && scrollY < innerHeight * 1.2) {
      const y = clamp(scrollY * .09, 0, 65);
      visual.style.translate = `0 ${y}px`;
    }
    ticking = false;
  };
  addEventListener('scroll', () => {
    if (!ticking) { requestAnimationFrame(paintScroll); ticking = true; }
  }, { passive: true });
  paintScroll();

  home.querySelectorAll('.btn').forEach(button => {
    button.classList.add('magnetic');
  });

  const counters = home.querySelectorAll('.hero-stat .k');
  counters.forEach(counter => {
    const match = counter.textContent.trim().match(/^(\d+)(.*)$/);
    if (!match) return;
    const target = Number(match[1]), suffix = match[2];
    counter.textContent = `0${suffix}`;
    const start = performance.now() + 950;
    const animate = now => {
      const p = clamp((now - start) / 900, 0, 1);
      const eased = 1 - Math.pow(1 - p, 3);
      counter.textContent = `${Math.round(target * eased)}${suffix}`;
      if (p < 1) requestAnimationFrame(animate);
    };
    requestAnimationFrame(animate);
  });
})();


/* Trainer workflow controls: fast day navigation, view switch, search and exports. */
(() => {
  'use strict';
  const boot = () => {
    const daybar = document.getElementById('tDaybar');
    if (daybar && !document.getElementById('trainerCalendarTools')) {
      const tools = document.createElement('div');
      tools.id = 'trainerCalendarTools';
      tools.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px';
      tools.innerHTML = `
        <button class="btn ghost sm" id="trainerPrevDay" type="button">← Previous</button>
        <button class="btn ghost sm" id="trainerToday" type="button">Today</button>
        <button class="btn ghost sm" id="trainerNextDay" type="button">Next →</button>
        <span style="flex:1"></span>
        <button class="btn ghost sm" id="trainerViewToggle" type="button">Day view</button>
        <button class="btn sm" id="trainerAddBooking" type="button">＋ Add appointment</button>`;
      daybar.parentNode.insertBefore(tools, daybar);
      document.getElementById('trainerPrevDay').onclick = () => window.setTDay?.(Math.max(0,(state.trainerDay||0)-1));
      document.getElementById('trainerToday').onclick = () => window.setTDay?.(0);
      document.getElementById('trainerNextDay').onclick = () => window.setTDay?.(Math.min(DAYS.length-1,(state.trainerDay||0)+1));
      document.getElementById('trainerAddBooking').onclick = () => window.openQuickBooking?.();
      document.getElementById('trainerViewToggle').onclick = function(){
        const week=document.getElementById('tWeekCalendar');
        const day=document.getElementById('tDayCalendar');
        const showingWeek=!week || week.style.display!=='none';
        if(week)week.style.display=showingWeek?'none':'';
        if(day)day.style.display=showingWeek?'':'none';
        this.textContent=showingWeek?'Week view':'Day view';
      };
    }

    const clients = document.getElementById('tClients');
    if (clients && !document.getElementById('trainerClientSearch')) {
      const search=document.createElement('input');
      search.id='trainerClientSearch';
      search.type='search';
      search.placeholder='Search clients by name, email or phone…';
      search.style.cssText='width:100%;margin:0 0 14px;padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:var(--paper)';
      clients.parentNode.insertBefore(search,clients);
      search.oninput=()=>{
        const q=search.value.trim().toLowerCase();
        clients.querySelectorAll('.client-card').forEach(card=>{
          card.style.display=!q||card.textContent.toLowerCase().includes(q)?'':'none';
        });
      };
    }

    const manage=document.getElementById('tp-manage');
    if(manage && !document.getElementById('trainerDataTools')){
      const box=document.createElement('section');
      box.id='trainerDataTools';
      box.className='mg-card';
      box.style.marginTop='18px';
      box.innerHTML=`<div class="ms-head"><h3>Data protection & exports</h3></div>
        <p class="ms-note">Daily protected server backups are retained for 30 days. Downloads require the trainer session.</p>
        <div style="display:flex;flex-wrap:wrap;gap:10px">
          <button class="btn ghost sm" id="downloadBackup" type="button">Download database backup</button>
          <button class="btn ghost sm" id="downloadGhsClaims" type="button">Export GHS claims</button>
          <button class="btn ghost sm" id="checkEmailDelivery" type="button">Check email delivery</button>
        </div><div id="emailDeliverySummary" class="ms-note" style="margin-top:12px"></div>`;
      manage.appendChild(box);
      const download=async(path,name)=>{
        try{
          const response=await fetch(BACKEND+path,{headers:{Authorization:'Bearer '+trainerToken}});
          if(!response.ok)throw new Error(await response.text());
          const a=document.createElement('a');a.href=URL.createObjectURL(await response.blob());a.download=name;a.click();
          setTimeout(()=>URL.revokeObjectURL(a.href),1000);
        }catch(e){toast(e.message||'Download failed','Trainer');}
      };
      document.getElementById('downloadBackup').onclick=()=>download('/backup','rebalance-backup.db');
      document.getElementById('downloadGhsClaims').onclick=()=>download('/trainer/ghs-claims.csv','rebalance-ghs-claims.csv');
      document.getElementById('checkEmailDelivery').onclick=async()=>{
        const out=document.getElementById('emailDeliverySummary');
        out.textContent='Checking…';
        try{
          const data=await api('/trainer/email-delivery?limit=100',{token:trainerToken});
          const failed=(data.emails||[]).filter(x=>x.status!=='sent');
          out.innerHTML=failed.length
            ? '<b>'+failed.length+' email(s) need attention.</b> Latest: '+failed.slice(0,3).map(x=>x.subject+' — '+x.status).join(' · ')
            : '<b>All recent emails were delivered successfully.</b>';
        }catch(e){out.textContent='Could not load email delivery history.';}
      };
    }
  };
  document.addEventListener('click',e=>{
    if(e.target.closest('[data-tp],#trainerBack'))setTimeout(boot,0);
  });
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);
  else boot();
})();


/* Contextual trainer diary: time → clients → client record. */
(() => {
  'use strict';
  let selectedTime = '';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

  function slotsBox(){ return document.getElementById('tSlots'); }
  function detailsTitle(){ return document.querySelector('#tp-sched .day-detail-title'); }

  function closeAppointment(){
    selectedTime='';
    slotsBox()?.classList.remove('context-open');
    detailsTitle()?.classList.remove('context-open');
    document.querySelectorAll('#tSlots .classcard').forEach(card=>card.hidden=true);
    document.querySelectorAll('#tDayCalendar .dc-event').forEach(button=>button.classList.remove('selected'));
  }

  function openAppointment(time, button){
    selectedTime=time;
    document.querySelectorAll('#tDayCalendar .dc-event').forEach(item=>item.classList.toggle('selected',item===button));
    const day=(typeof store!=='undefined'&&typeof state!=='undefined')?(store[state.trainerDay]||{}):{};
    const session=day[time];
    const clients=(session?.spots||[]).map((client,index)=>({client,index})).filter(x=>x.client&&typeof x.client==='object');
    closeAppointment();
    if(clients.length===1){
      showClient(time,clients[0].index);
      return;
    }
    if(!clients.length)return;
    let modal=document.getElementById('trainerClientDrawer');
    if(!modal){modal=document.createElement('div');modal.id='trainerClientDrawer';modal.className='trainer-client-drawer';document.body.appendChild(modal);}
    modal.innerHTML='<button class="drawer-backdrop" aria-label="Close clients"></button><aside role="dialog" aria-modal="true" aria-label="Clients at '+esc(time)+'">'+
      '<div class="drawer-head"><div><small>'+esc(time)+' · '+esc(session.cls||'SESSION')+'</small><h2>Choose a client</h2></div><button class="drawer-close" aria-label="Close">×</button></div>'+
      '<div class="drawer-client-list">'+clients.map(x=>'<button type="button" data-client-index="'+x.index+'"><span>'+esc(x.client.name||'Client')+'</span><small>'+esc(x.client.pay==='ghs'?'GESY':x.client.pay||'Paid')+'</small><b>View →</b></button>').join('')+'</div></aside>';
    modal.classList.add('open');
    modal.querySelectorAll('.drawer-close,.drawer-backdrop').forEach(el=>el.onclick=()=>modal.classList.remove('open'));
    modal.querySelectorAll('[data-client-index]').forEach(el=>el.onclick=()=>showClient(time,Number(el.dataset.clientIndex)));
  }

  function showClient(time, index){
    const day=(typeof store!=='undefined'&&typeof state!=='undefined')?(store[state.trainerDay]||{}):{};
    const session=day[time];
    const client=session?.spots?.[index];
    if(!client||typeof client!=='object')return;
    let modal=document.getElementById('trainerClientDrawer');
    if(!modal){
      modal=document.createElement('div');
      modal.id='trainerClientDrawer';
      modal.className='trainer-client-drawer';
      document.body.appendChild(modal);
    }
    const attendance=client.attended===true?'Here':client.attended===false?'No-show':'Not recorded';
    const payment=client.pay==='ghs'?'GESY':client.pay==='viva'?'Viva Wallet':client.pay==='cash'?'Cash':client.pay==='credit'?'Package':client.pay||'Paid';
    modal.innerHTML='<button class="drawer-backdrop" aria-label="Close client details"></button><aside role="dialog" aria-modal="true" aria-label="Client details">'+
      '<div class="drawer-head"><div><small>CLIENT RECORD</small><h2>'+esc(client.name||'Client')+'</h2></div><button class="drawer-close" aria-label="Close">×</button></div>'+
      '<div class="drawer-session"><span>'+esc(time)+'</span><b>'+esc(session.cls||'Session')+'</b><em>'+(session.private?'Private':'Group')+'</em></div>'+
      '<dl class="drawer-facts">'+
      (client.email?'<div><dt>Email</dt><dd><a href="mailto:'+esc(client.email)+'">'+esc(client.email)+'</a></dd></div>':'')+
      (client.phone?'<div><dt>Phone</dt><dd><a href="tel:'+esc(client.phone)+'">'+esc(client.phone)+'</a></dd></div>':'')+
      '<div><dt>Payment</dt><dd>'+esc(payment)+'</dd></div><div><dt>Attendance</dt><dd>'+esc(attendance)+'</dd></div></dl>'+
      '<p>Close this panel to return to the appointment.</p></aside>';
    modal.classList.add('open');
    modal.querySelectorAll('.drawer-close,.drawer-backdrop').forEach(el=>el.onclick=()=>modal.classList.remove('open'));
  }

  function bind(){
    const calendar=document.getElementById('tDayCalendar'), slots=slotsBox();
    if(!calendar||!slots)return;
    if(!calendar.dataset.contextBound){
      calendar.dataset.contextBound='1';
      calendar.addEventListener('click',event=>{
        const button=event.target.closest('.dc-event');
        if(!button)return;
        event.preventDefault();
        event.stopImmediatePropagation();
        const track=button.closest('.dc-track');
        const time=track?.previousElementSibling?.textContent.trim();
        if(time)openAppointment(time,button);
      },true);
    }
    if(!slots.dataset.contextBound){
      slots.dataset.contextBound='1';
      slots.addEventListener('click',event=>{
        const row=event.target.closest('.spot-row.taken');
        if(!row||event.target.closest('button'))return;
        const card=row.closest('.classcard'), time=card?.dataset.time;
        const attendanceButton=row.querySelector('.att-btn');
        const match=attendanceButton?.getAttribute('onclick')?.match(/,\s*(\d+)\s*,\s*true\s*\)/);
        if(time&&match)showClient(time,Number(match[1]));
      });
    }
    if(!selectedTime)closeAppointment();
    else openAppointment(selectedTime,document.querySelector('#tDayCalendar .dc-event.selected'));
  }

  const observer=new MutationObserver(mutations=>{
    if(mutations.some(m=>m.target.id==='tSlots'||m.target.id==='tDayCalendar')){
      requestAnimationFrame(bind);
    }
  });
  const start=()=>{
    const root=document.getElementById('tp-sched');
    if(!root)return;
    observer.observe(root,{childList:true,subtree:true});
    bind();
    document.getElementById('tDaybar')?.addEventListener('click',()=>{selectedTime='';setTimeout(closeAppointment,0)});
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();


/* Fast, explicit password recovery — no silent waiting or bottom toast dependency. */
(() => {
  'use strict';
  const statusLine=()=>{
    let line=document.getElementById('resetStatus');
    if(!line){
      line=document.createElement('div');
      line.id='resetStatus';
      line.className='reset-status';
      document.getElementById('btnSendReset')?.insertAdjacentElement('afterend',line);
    }
    return line;
  };
  const setStatus=(message,type='working')=>{
    const line=statusLine(); if(!line)return;
    line.className='reset-status '+type;
    line.textContent=message;
  };
  const request=async(path,body,timeout=15000)=>{
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),timeout);
    try{
      const response=await fetch(BACKEND+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});
      let data={};try{data=await response.json();}catch(e){}
      if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Request failed. Please try again.');
      return data;
    }catch(error){
      if(error.name==='AbortError')throw new Error('The server did not respond. Please try again.');
      throw error;
    }finally{clearTimeout(timer);}
  };

  window.sendResetCode=async function(){
    if(resetBusy)return;
    const email=(document.getElementById('fp-email')?.value||'').trim();
    if(!email){setStatus('Enter your email address.','error');document.getElementById('fp-email')?.focus();return;}
    resetEmail=email;resetBusy=true;
    const button=document.getElementById('btnSendReset');
    if(button){button.disabled=true;button.textContent='Sending code…';}
    setStatus('Contacting the secure account service…');
    try{
      let result={};
      if(BACKEND)result=await request('/password/forgot',{email});
      else{demoResetCode=String(Math.floor(100000+Math.random()*900000));result.test_code=demoResetCode;}
      if(result.test_code)document.getElementById('fpDemo').innerHTML='Test code: <b>'+result.test_code+'</b>';
      document.getElementById('fpEmailShown').textContent=email;
      showAuthPane('c-reset');
      let top=document.getElementById('resetFormStatus');
      if(!top){top=document.createElement('div');top.id='resetFormStatus';top.className='reset-status success';document.querySelector('#c-reset .verify-sub')?.insertAdjacentElement('afterend',top);}
      top.textContent='Code sent. Check your inbox and spam folder.';
      document.querySelector('#resetInputs input')?.focus();
      startCooldown('btnSendReset',30,'Send code');
      startCooldown('btnResendReset',30,'Resend code');
    }catch(error){
      setStatus(error.message||'Could not send the code. Please try again.','error');
    }finally{
      resetBusy=false;
      if(button&&!button.classList.contains('cooling')){button.disabled=false;button.textContent='Send code';}
    }
  };
})();
