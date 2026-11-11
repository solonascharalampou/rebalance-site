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

  let pointerTick = false;
  addEventListener('pointermove', event => {
    if (pointerTick) return;
    pointerTick = true;
    requestAnimationFrame(() => {
      document.documentElement.style.setProperty('--mouse-x', `${event.clientX}px`);
      document.documentElement.style.setProperty('--mouse-y', `${event.clientY}px`);
      pointerTick = false;
    });
  }, { passive: true });

  home.querySelectorAll('.btn').forEach(button => {
    button.classList.add('magnetic');
    button.addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      const rect = button.getBoundingClientRect();
      const x = (event.clientX - rect.left - rect.width / 2) * .12;
      const y = (event.clientY - rect.top - rect.height / 2) * .18;
      button.style.transform = `translate(${x}px,${y - 2}px)`;
    });
    button.addEventListener('pointerleave', () => button.style.transform = '');
  });

  home.querySelectorAll('.approach .c').forEach(card => {
    card.addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      const r = card.getBoundingClientRect();
      const rx = ((event.clientY - r.top) / r.height - .5) * -5;
      const ry = ((event.clientX - r.left) / r.width - .5) * 5;
      card.style.transform = `perspective(700px) rotateX(${rx}deg) rotateY(${ry}deg) translateY(-4px)`;
    });
    card.addEventListener('pointerleave', () => card.style.transform = '');
  });

  const visit = home.querySelector('.visit-card');
  if (visit) {
    visit.addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      const r = visit.getBoundingClientRect();
      visit.style.setProperty('--tilt-x', `${((event.clientX-r.left)/r.width-.5)*3.5}deg`);
      visit.style.setProperty('--tilt-y', `${((event.clientY-r.top)/r.height-.5)*-3.5}deg`);
    });
    visit.addEventListener('pointerleave', () => {
      visit.style.setProperty('--tilt-x', '0deg');
      visit.style.setProperty('--tilt-y', '0deg');
    });
  }

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
