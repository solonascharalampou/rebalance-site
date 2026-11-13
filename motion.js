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
