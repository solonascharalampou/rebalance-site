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

/* Trainer portal data bootstrap.
   The portal used to render its tabs before the live schedule request finished,
   so Schedule, Clients and Earnings looked empty until the trainer changed the
   selected date. Keep one shared refresh in flight and repaint every data-backed
   tab only after the API has populated the browser store. */
(() => {
  'use strict';
  let trainerRefresh = null;

  // The original implementation requested every day at once. Each schedule
  // request may create missing SQLite rows, so six concurrent writes can wait
  // on one another until the browser's 45-second timeout. Load days in sequence:
  // it is fast on this local server and cannot deadlock the diary on first open.
  if (typeof window.syncTrainerDay === 'function') {
    window.syncTrainerWeek = async function () {
      if (!window.BACKEND && typeof BACKEND !== 'undefined' && !BACKEND) {
        window.renderWeekOverview?.();
        return;
      }
      const days = (typeof DAYS !== 'undefined' && DAYS) || [];
      for (let i = 0; i < days.length; i++) await window.syncTrainerDay(i);
      window.renderWeekOverview?.();
    };
  }

  const loading = (id, message) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = `<div class="empty"><b>${message}</b><span>Please wait a moment.</span></div>`;
  };

  const refreshTrainerData = () => {
    if (trainerRefresh) return trainerRefresh;
    if (typeof window.syncTrainerWeek !== 'function') return Promise.resolve();
    trainerRefresh = Promise.resolve(window.syncTrainerWeek())
      .then(() => {
        window.renderTDays?.();
        window.renderTSlots?.();
        window.renderWeekOverview?.();
        window.renderClients?.();
        window.renderEarnings?.();
      })
      .finally(() => { trainerRefresh = null; });
    return trainerRefresh;
  };

  const originalEnter = window.enterTrainerPortal;
  if (typeof originalEnter === 'function') {
    window.enterTrainerPortal = function (...args) {
      const result = originalEnter.apply(this, args);
      loading('tSlots', 'Loading today’s appointments…');
      loading('tDayCalendar', 'Loading today’s calendar…');
      loading('tClients', 'Loading clients…');
      refreshTrainerData();
      return result;
    };
  }

  const originalTab = window.trainTab;
  if (typeof originalTab === 'function') {
    window.trainTab = function (tab, ...args) {
      const result = originalTab.call(this, tab, ...args);
      if (tab === 'sched') loading('tSlots', 'Refreshing appointments…');
      if (tab === 'clients') loading('tClients', 'Refreshing clients…');
      if (tab === 'sched' || tab === 'clients' || tab === 'earnings') refreshTrainerData();
      return result;
    };
  }
})();
