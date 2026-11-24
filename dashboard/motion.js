/* ===================================================================
   Motion.

   Three rules this file keeps to:

   1. Nothing here may change what the page says. The numbers are
      rendered from the eval's JSON by the page's own script; motion
      only counts up to a figure that is already in the DOM, and puts
      the exact original text back at the end of every count.
   2. Everything degrades. If GSAP or Lenis fail to load, if the
      browser has no IntersectionObserver, or if the visitor asks for
      reduced motion, the page is still complete and still readable --
      it just sits still.
   3. The page builds itself asynchronously, so nothing can be wired
      once at load. A MutationObserver re-scans what the renderers
      inject and wires only what is new.
   =================================================================== */

(function () {
  'use strict';

  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const gsap = window.gsap;
  const ST = window.ScrollTrigger;
  const hasGsap = !!(gsap && ST);
  if (hasGsap) gsap.registerPlugin(ST);

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.prototype.slice.call((r || document).querySelectorAll(s));
  const seen = new WeakSet();

  /* ---- smooth scrolling ------------------------------------------
     Lenis drives the scroll position; ScrollTrigger is told to read
     from it rather than from the native scroll event, or the two
     disagree by a frame and everything judders. */
  let lenis = null;
  if (!reduced && window.Lenis) {
    lenis = new window.Lenis({
      duration: 1.05,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
      touchMultiplier: 1.6,
    });
    // Exposed so the scroll position can be driven from the console
    // (and from the browser checks that verify the pinned sequence);
    // nothing on the page reads it.
    window.__lenis = lenis;
    if (hasGsap) {
      lenis.on('scroll', ST.update);
      gsap.ticker.add((time) => lenis.raf(time * 1000));
      gsap.ticker.lagSmoothing(0);
    } else {
      const raf = (time) => { lenis.raf(time); requestAnimationFrame(raf); };
      requestAnimationFrame(raf);
    }
    // In-page links have to go through Lenis, or they jump while it
    // is still easing somewhere else.
    document.addEventListener('click', (e) => {
      const a = e.target.closest('a[href^="#"]');
      if (!a) return;
      const id = a.getAttribute('href');
      if (!id || id === '#') return;
      const el = document.querySelector(id);
      if (!el) return;
      e.preventDefault();
      lenis.scrollTo(el, { offset: -72 });
      history.replaceState(null, '', id);
    });
  }

  /* ---- the headline, word by word -------------------------------- */
  function splitHeadline(h) {
    if (!h || seen.has(h)) return;
    seen.add(h);
    const words = h.textContent.trim().split(/\s+/);
    h.textContent = '';
    words.forEach((w, i) => {
      const box = document.createElement('span');
      box.className = 'rev-word';
      const inner = document.createElement('i');
      inner.textContent = w;
      box.appendChild(inner);
      h.appendChild(box);
      if (i < words.length - 1) h.appendChild(document.createTextNode(' '));
    });
    if (reduced || !hasGsap) return;
    gsap.from(h.querySelectorAll('.rev-word > i'), {
      yPercent: 108,
      duration: 1.05,
      ease: 'power4.out',
      stagger: 0.045,
      delay: 0.12,
    });
  }

  /* ---- generic reveals -------------------------------------------
     IntersectionObserver rather than one ScrollTrigger per element:
     the page injects a few hundred nodes and this stays cheap. */
  const io = 'IntersectionObserver' in window && !reduced
    ? new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return;
        const el = en.target;
        io.unobserve(el);
        el.style.transition = 'opacity .7s cubic-bezier(.22,1,.36,1), transform .7s cubic-bezier(.22,1,.36,1)';
        el.style.transitionDelay = (el.dataset.delay || 0) + 'ms';
        el.style.opacity = '1';
        el.style.transform = 'none';
        if (el.dataset.count) countUp(el);
        if (el.dataset.bar) growBars(el);
      });
      // threshold 0: a block taller than the viewport can never reach a
      // fractional threshold, and the first run of this page left whole
      // sections stuck at opacity 0 when scrolled past quickly.
    }, { rootMargin: '0px 0px -5% 0px', threshold: 0 })
    : null;

  /* A failsafe. Reveals are an enhancement, so no path through this
     file may leave content invisible: anything still hidden once it
     has reached the fold is shown, observer or no observer. */
  function sweep() {
    $$('.reveal').forEach((el) => {
      if (el.style.opacity === '1') return;
      const r = el.getBoundingClientRect();
      if (r.top > window.innerHeight * 1.05) return;
      if (io) io.unobserve(el);
      el.style.transition = 'opacity .5s cubic-bezier(.22,1,.36,1), transform .5s cubic-bezier(.22,1,.36,1)';
      el.style.opacity = '1';
      el.style.transform = 'none';
      if (el.dataset.count) countUp(el);
      if (el.dataset.bar) growBars(el);
    });
  }

  function reveal(el, delay) {
    if (!el || seen.has(el)) return;
    seen.add(el);
    if (!io) { if (el.dataset.count) countUp(el); return; }
    el.classList.add('reveal');
    if (delay) el.dataset.delay = String(delay);
    io.observe(el);
  }

  /* ---- counting up ------------------------------------------------
     Only the first text node is touched, so markup inside a figure
     (the unit span, a denominator) is left exactly as the renderer
     wrote it. The original string is restored on the last frame, so
     formatting can never drift from what the JSON said. */
  const NUM = /^(\s*[^\d-]*)(-?[\d,]+(?:\.\d+)?)(.*)$/s;

  function countable(el) {
    const node = Array.prototype.find.call(el.childNodes, (n) => n.nodeType === 3 && NUM.test(n.nodeValue));
    if (!node) return null;
    const m = node.nodeValue.match(NUM);
    const target = parseFloat(m[2].replace(/,/g, ''));
    if (!isFinite(target) || Math.abs(target) > 1e9) return null;
    return { node, original: node.nodeValue, prefix: m[1], suffix: m[3], target,
      decimals: (m[2].split('.')[1] || '').length, grouped: m[2].indexOf(',') >= 0 };
  }

  function countUp(el) {
    const c = el._count || countable(el);
    if (!c || reduced || !hasGsap) return;
    const o = { v: 0 };
    gsap.to(o, {
      v: c.target,
      duration: 1.15,
      ease: 'power2.out',
      onUpdate() {
        const n = c.decimals ? o.v.toFixed(c.decimals) : String(Math.round(o.v));
        c.node.nodeValue = c.prefix + (c.grouped ? Number(n).toLocaleString('en-IN') : n) + c.suffix;
      },
      onComplete() { c.node.nodeValue = c.original; },
    });
  }

  function markCounters(root) {
    $$('.fig, .ver-fig, .cite-n, .touch td', root).forEach((el) => {
      // Once only. The page renders asynchronously, so this runs again
      // on every mutation -- and a second pass would read back the zero
      // this one wrote and take *that* for the target, which is how
      // every figure on the page came to settle on 0.
      if (el.dataset.count) return;
      const c = countable(el);
      if (!c) return;
      el._count = c;
      el.dataset.count = '1';
      // Deliberately not zeroed here. A figure that is never scrolled
      // to must still read what the JSON said: on a page whose whole
      // argument is that its numbers are real, a decorative 0 left
      // behind by a failed observer would be a lie.
      reveal(el);
    });
  }

  /* ---- bars -------------------------------------------------------- */
  function growBars(el) {
    $$('.bar i', el).forEach((bar) => {
      const w = bar.style.width;
      if (!w || reduced || !hasGsap) return;
      gsap.fromTo(bar, { width: '0%' }, { width: w, duration: 0.9, ease: 'power3.out' });
    });
  }

  /* ---- the eleven nodes: the connector draws as you reach it ------ */
  function wireTimeline(root) {
    if (reduced || !hasGsap) return;
    $$('.tl-node', root).forEach((n) => {
      if (seen.has(n) || n.dataset.drawn) return;
      n.dataset.drawn = '1';
      gsap.from(n, {
        scrollTrigger: { trigger: n, start: 'top 88%', once: true },
        opacity: 0, y: 14, duration: 0.55, ease: 'power3.out',
      });
    });
  }

  /* ---- cards: a sheen that follows the pointer -------------------- */
  function wireCards(root) {
    $$('.card, .run-card, .metric', root).forEach((c) => {
      if (c.dataset.sheen) return;
      c.dataset.sheen = '1';
      c.addEventListener('pointermove', (e) => {
        const r = c.getBoundingClientRect();
        c.style.setProperty('--mx', ((e.clientX - r.left) / r.width) * 100 + '%');
        c.style.setProperty('--my', ((e.clientY - r.top) / r.height) * 100 + '%');
      });
    });
  }

  /* ---- the ticker: built from the rules the page parsed ----------- */
  function fillTicker() {
    const row = $('#ticker-row');
    if (!row || row.dataset.filled) return;
    const rules = $$('.rule');
    if (!rules.length) return;
    row.dataset.filled = '1';
    const items = rules.map((r) => {
      const id = ($('.rule-id', r) || {}).textContent || '';
      const title = ($('h3', r) || {}).textContent || '';
      return '<span><b>' + id.trim() + '</b>' + title.trim() + '</span>';
    }).join('');
    // Two copies, so the loop has something to slide into.
    row.innerHTML = '<div>' + items + '</div><div aria-hidden="true">' + items + '</div>';
  }

  /* ---- section thread and the masthead ---------------------------- */
  function wireThread() {
    const thread = $('.thread');
    const mast = $('.mast');
    // The masthead nav is the page's own wireNav(); this only drives the
    // thread, so the two never write the same attribute.
    const links = thread ? $$('a', thread) : [];
    const sections = $$('main section[id]');
    if (!sections.length) return;

    const setCurrent = (id) => {
      links.forEach((a) => a.setAttribute('aria-current', String(a.getAttribute('href') === '#' + id)));
    };

    if ('IntersectionObserver' in window) {
      const obs = new IntersectionObserver((entries) => {
        entries.forEach((en) => { if (en.isIntersecting) setCurrent(en.target.id); });
      }, { rootMargin: '-45% 0px -50% 0px' });
      sections.forEach((s) => obs.observe(s));
    }

    let idle = 0;
    const onScroll = () => {
      const y = window.scrollY || document.documentElement.scrollTop;
      if (mast) mast.classList.toggle('is-stuck', y > 8);
      if (thread) thread.classList.toggle('on', y > window.innerHeight * 0.6);
      clearTimeout(idle);
      idle = setTimeout(sweep, 180);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  /* ---- §03: the approval, pinned and played ----------------------
     Four beats: approved, edited while it waited, read back, void.
     Every element moved here was rendered from the run's own trace,
     and the two stamps take their words from rows already on screen,
     so the sequence cannot say anything the record does not. */
  function wireApprovalAct() {
    if (reduced || !hasGsap) return;
    // Pinning costs the visitor three screens of scroll and needs the
    // whole card to fit above the fold. On a phone it does neither, so
    // the section stays the plain document it already reads as.
    // 760px is measured, not guessed: at 768 the finished card ends
    // one pixel above the fold, and below that it would be cropped.
    if (!window.matchMedia('(min-width: 981px) and (min-height: 760px)').matches) return;
    const sec = $('#approval');
    const wrap = sec && sec.querySelector('.wrap');
    const rcpt = sec && sec.querySelector('.receipt');
    if (!sec || !wrap || !rcpt || rcpt.dataset.act) return;

    const row = (k) => rcpt.querySelector('[data-row="' + k + '"]');
    const diff = rcpt.querySelector('.diff');
    const stored = row('digest-stored');
    const approved = row('approver');
    // Without the digest pair there is no sequence to play; leave the
    // section as the plain document it already is.
    if (!diff || !stored || !row('digest-approved')) return;
    rcpt.dataset.act = '1';

    const opening = ['po', 'value', 'digest-approved', 'approver', 'limit'].map(row).filter(Boolean);
    const closing = ['digests', 'outcome', 'rules'].map(row).filter(Boolean);
    const verdict = rcpt.querySelector('.verdict');
    const plain = sec.querySelector('.plain-diff');
    const say = sec.querySelector('.receipt-say');
    const diffLines = $$('.diff-l', diff);

    // The stamps: approved takes the approver's id from the row that
    // names them; void takes its rule ids from the chips already there.
    const who = approved ? (approved.lastElementChild.textContent.split('·')[0] || '').trim() : '';
    const ruleIds = $$('[data-row="rules"] .chip', rcpt).map((c) => c.textContent.trim()).join(' ');
    rcpt.style.position = 'relative';
    const okStamp = document.createElement('p');
    okStamp.className = 'stamp stamp-ok';
    okStamp.textContent = who ? 'Approved · ' + who : 'Approved';
    const voidStamp = document.createElement('p');
    voidStamp.className = 'stamp stamp-void';
    voidStamp.textContent = ruleIds ? 'Approval void · ' + ruleIds : 'Approval void';
    rcpt.appendChild(okStamp);
    rcpt.appendChild(voidStamp);

    // A beat counter, so a pinned section says where you are in it.
    const beats = document.createElement('p');
    beats.className = 'beats';
    beats.setAttribute('aria-hidden', 'true');
    beats.innerHTML = ['Approved', 'Edited while it waited', 'Read back', 'Void']
      .map((b, i) => '<span>' + String(i + 1).padStart(2, '0') + ' ' + b + '</span>').join('');
    const col = sec.querySelector('.receipt-wrap > div:last-child');
    if (col) col.insertBefore(beats, col.firstChild);
    const beatEls = $$('span', beats);
    const setBeat = (n) => beatEls.forEach((b, i) => b.classList.toggle('on', i <= n));

    // The digest arrives a character at a time, left to right. The
    // first instinct was to churn random hex and settle on the real
    // value, which looks better and is a lie: stop scrolling half way
    // and the page is showing a hash the ERP never returned. What is
    // on screen is always a true prefix of the real digest; the rest
    // is dots, which claim nothing.
    const digestCell = stored.lastElementChild;
    const finalDigest = digestCell.textContent;
    const typeOut = (p) => {
      const n = finalDigest.length;
      const shown = Math.round(n * Math.max(0, Math.min(1, p)));
      digestCell.textContent = finalDigest.slice(0, shown) + '·'.repeat(n - shown);
    };

    try {
      // The opening rows and the approved stamp are *not* hidden. The
      // order really was approved, and a pinned section whose first
      // frame is an empty card looks broken rather than cinematic.
      gsap.set([].concat(closing, diffLines, [diff, stored, verdict, plain, say, voidStamp])
        .filter(Boolean), { autoAlpha: 0 });
      gsap.set(diff, { height: 0, overflow: 'hidden' });
      gsap.set(voidStamp, { scale: 1.5 });

      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: sec,
          start: 'top top+=64',
          end: '+=320%',
          pin: wrap,
          pinSpacing: true,
          scrub: 0.8,
          anticipatePin: 1,
          onUpdate(self) { setBeat(Math.min(3, Math.floor(self.progress * 4))); },
        },
      });

      // 01 approved: already on screen; the beat is the reading of it
      tl.to(opening, { duration: 0.9 }, 0)
      // 02 edited while it waited
        .to(diff, { autoAlpha: 1, height: 'auto', duration: 0.5 }, 1.0)
        .to(diffLines, { autoAlpha: 1, x: 0, duration: 0.3, stagger: 0.16 }, 1.1)
        .from(diffLines, { x: -14, duration: 0.3, stagger: 0.16 }, 1.1)
        .to(plain, { autoAlpha: 1, duration: 0.4 }, 1.5)
      // 03 read back
        .to(stored, { autoAlpha: 1, duration: 0.3 }, 2.1)
        .to({ p: 0 }, { p: 1, duration: 0.9, ease: 'none', onUpdate() { typeOut(this.targets()[0].p); },
          onComplete() { digestCell.textContent = finalDigest; } }, 2.2)
        .to(row('digests') || {}, { autoAlpha: 1, duration: 0.3 }, 2.9)
        .add(() => { stored.classList.add('hot'); if (row('digests')) row('digests').classList.add('hot'); }, 2.9)
      // 04 void
        .to(okStamp, { autoAlpha: 0.25, duration: 0.3 }, 3.1)
        .to(voidStamp, { autoAlpha: 1, scale: 1, duration: 0.45, ease: 'back.out(2)' }, 3.2)
        .to([row('outcome'), row('rules'), verdict].filter(Boolean),
          { autoAlpha: 1, y: 0, duration: 0.35, stagger: 0.14 }, 3.4)
        .to(say, { autoAlpha: 1, duration: 0.4 }, 3.7);

      // Scrubbing backwards past the beat must undo it, or the row
      // stays red while the digests still agree.
      tl.eventCallback('onUpdate', () => {
        if (tl.progress() < 0.56) {
          stored.classList.remove('hot');
          if (row('digests')) row('digests').classList.remove('hot');
        }
      });
    } catch (e) {
      // Any failure here must leave the section readable, not half-lit.
      [].concat(closing, diffLines, [diff, stored, verdict, plain, say]).filter(Boolean)
        .forEach((el) => { el.style.cssText = ''; });
      okStamp.remove();
      voidStamp.remove();
      return;
    }
    sec.classList.add('is-act');
  }

  /* ---- the opening curtain ---------------------------------------
     Built in script and removed on a timer that does not depend on
     anything loading, so the worst case is a blank half second, never
     a page the visitor cannot get past. */
  function wireCurtain() {
    if (reduced || !hasGsap) return;
    const c = document.createElement('div');
    c.className = 'curtain';
    c.setAttribute('aria-hidden', 'true');
    c.innerHTML = '<p>ProcureFlow · evaluation record</p>';
    document.body.appendChild(c);
    const lift = () => gsap.to(c, {
      autoAlpha: 0, duration: 0.7, ease: 'power2.inOut', delay: 0.12,
      onComplete: () => c.remove(),
    });
    if (document.readyState === 'complete') lift();
    else window.addEventListener('load', lift);
    setTimeout(() => { if (c.isConnected) lift(); }, 2200);   // never stuck
  }

  /* ---- section rules draw as you approach them -------------------- */
  function wireRules(root) {
    if (reduced || !hasGsap) return;
    $$('.sec-head', root).forEach((h) => {
      if (h.dataset.drawn) return;
      h.dataset.drawn = '1';
      const line = document.createElement('i');
      line.className = 'draw';
      line.setAttribute('aria-hidden', 'true');
      h.insertBefore(line, h.firstChild);
      gsap.to(line, {
        scrollTrigger: { trigger: h, start: 'top 92%', end: 'top 55%', scrub: 0.7 },
        scaleX: 1, ease: 'none',
      });
    });
  }

  /* ---- the progress hairline -------------------------------------- */
  function wireProgress() {
    const bar = document.createElement('div');
    bar.className = 'prog';
    bar.setAttribute('aria-hidden', 'true');
    document.body.appendChild(bar);
    const set = () => {
      const h = document.documentElement.scrollHeight - window.innerHeight;
      const p = h > 0 ? Math.min(1, (window.scrollY || 0) / h) : 0;
      bar.style.transform = 'scaleX(' + p + ')';
    };
    window.addEventListener('scroll', set, { passive: true });
    window.addEventListener('resize', set);
    set();
  }

  /* ---- the pointer ring ------------------------------------------- */
  function wireRing() {
    if (reduced || !window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
    const ring = document.createElement('div');
    ring.className = 'ring';
    ring.setAttribute('aria-hidden', 'true');
    document.body.appendChild(ring);
    let x = 0, y = 0, rx = 0, ry = 0, on = false;
    window.addEventListener('pointermove', (e) => {
      x = e.clientX; y = e.clientY;
      if (!on) { on = true; rx = x; ry = y; ring.classList.add('on'); }
      const hot = !!e.target.closest('a, button, .tab, .card, summary, [role="tab"]');
      ring.classList.toggle('hot', hot);
    }, { passive: true });
    (function loop() {
      rx += (x - rx) * 0.22;
      ry += (y - ry) * 0.22;
      ring.style.transform = 'translate(' + (rx - 13) + 'px,' + (ry - 13) + 'px)';
      requestAnimationFrame(loop);
    }());
  }

  /* ---- the hero recedes as you leave it --------------------------- */
  function wireHero() {
    const hero = $('.hero .wrap');
    if (!hero || reduced || !hasGsap) return;
    gsap.to(hero, {
      scrollTrigger: { trigger: '.hero', start: 'top top', end: 'bottom 30%', scrub: 0.6 },
      opacity: 0.25, y: -40, ease: 'none',
    });
    $$('.sec-no').forEach((n) => {
      gsap.from(n, {
        scrollTrigger: { trigger: n.closest('.sec') || n, start: 'top 85%', end: 'top 35%', scrub: 0.8 },
        y: 26, ease: 'none',
      });
    });
  }

  /* ---- scan: called at load and whenever the page renders more ---- */
  function scan(root) {
    const r = root || document;
    $$('.sec-head, .cat, .card, .receipt, .metric, .ver, .rule, .limits li, .touch, .prov, .tabs, .run-card', r)
      .forEach((el, i) => reveal(el, Math.min(i, 6) * 40));
    markCounters(r);
    wireTimeline(r);
    wireCards(r);
    fillTicker();
    wireRules(r);
    wireApprovalAct();
    if (hasGsap) ST.refresh();
    // A refresh moves everything; anything already at the fold has to
    // be shown now, because the observer that would have caught it
    // fired against the old layout.
    sweep();
  }

  function boot() {
    wireCurtain();
    splitHeadline($('#hero-title'));
    wireThread();
    wireProgress();
    wireRing();
    wireHero();
    scan(document);

    const main = $('main');
    if (main && 'MutationObserver' in window) {
      let pending = 0;
      const mo = new MutationObserver(() => {
        clearTimeout(pending);
        pending = setTimeout(() => scan(document), 90);
      });
      mo.observe(main, { childList: true, subtree: true });
    }

    // The theme toggle rewrites nothing but the custom properties, so
    // the scene and the rest only need a nudge to re-measure.
    const btn = $('#theme-btn');
    if (btn && hasGsap) btn.addEventListener('click', () => setTimeout(() => ST.refresh(), 60));

    if (hasGsap) ST.addEventListener('refresh', sweep);
    window.addEventListener('resize', () => setTimeout(sweep, 120));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}());
