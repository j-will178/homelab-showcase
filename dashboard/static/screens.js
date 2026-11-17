/* Rack Dashboard - slideshow controller.
 *
 * Screens are <section class="screen" data-id=".." data-title=".."> elements.
 * Inactive screens stay laid out (visibility: hidden), so live screens like the
 * noise map keep running in the background.
 *
 * URL options: ?screen=smart (start screen)  ?interval=300 (seconds, 0 = off)
 * ?transition=wave|fade|none (see transition.js)
 * Keys: → / Space / PageDown next · ← / PageUp previous · 1-9 jump · R pause · F fullscreen
 * Emits a `screenchange` event on document with detail {id, index}.
 */
(() => {
  'use strict';
  const qs = new URLSearchParams(location.search);
  const screens = [...document.querySelectorAll('.screen')];
  const dots = document.getElementById('dots');
  const fill = document.getElementById('progressFill');
  const rotState = document.getElementById('rotState');
  const nav = document.getElementById('rotator');

  const S = {
    idx: Math.max(0, screens.findIndex(s => s.dataset.id === qs.get('screen'))),
    intervalMs: 300000,
    paused: false,
    shownAt: performance.now(),
  };

  screens.forEach((s, i) => {
    const b = document.createElement('button');
    b.className = 'scr';
    b.title = `${i + 1}. ${s.dataset.title}`;
    b.innerHTML = `<span class="pip"></span><span class="label">${s.dataset.title}</span>`;
    b.onclick = () => show(i);
    dots.appendChild(b);
  });

  // show(i): switch to screen i with the wave transition. The highlighted dot
  // and the rotation timer move right away; the screen itself switches at the
  // transition's midpoint (to whatever was picked last, if keys were mashed).
  function show(i, instant = false) {
    S.target = (i + screens.length) % screens.length;
    S.shownAt = performance.now();
    [...dots.children].forEach((d, j) => d.classList.toggle('on', j === S.target));
    wake();
    if (instant || S.target === S.idx) apply(S.target);
    else (window.waveTransition || (f => f()))(() => apply(S.target));
  }

  function apply(i) {
    S.idx = i;
    screens.forEach((s, j) => s.classList.toggle('active', j === S.idx));
    const id = screens[S.idx].dataset.id;
    document.body.dataset.screen = id;
    document.dispatchEvent(new CustomEvent('screenchange', { detail: { id, index: S.idx } }));
  }

  const cur = () => S.target ?? S.idx;

  const rotating = () => S.intervalMs > 0 && !S.paused && screens.length > 1;

  function updateState() {
    rotState.textContent = S.intervalMs <= 0 ? 'rotation off' : S.paused ? '❚❚ paused' : `every ${fmt(S.intervalMs / 1000)}`;
    nav.classList.toggle('paused', !rotating());
  }
  const fmt = s => s >= 60 ? `${+(s / 60).toFixed(1)} min` : `${s} s`;

  function tick(now) {
    if (rotating()) {
      const p = (now - S.shownAt) / S.intervalMs;
      if (p >= 1) show(cur() + 1);
      else fill.style.transform = `scaleX(${p})`;
    } else {
      fill.style.transform = 'scaleX(0)';
    }
    requestAnimationFrame(tick);
  }

  // The nav dims itself after a few seconds without input, so it doesn't distract on the wall.
  let wakeTimer;
  function wake() {
    nav.classList.add('awake');
    clearTimeout(wakeTimer);
    wakeTimer = setTimeout(() => nav.classList.remove('awake'), 4000);
  }
  document.addEventListener('mousemove', wake);

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT') return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const k = e.key;
    if (k === 'ArrowRight' || k === ' ' || k === 'PageDown') { e.preventDefault(); show(cur() + 1); }
    else if (k === 'ArrowLeft' || k === 'PageUp') { e.preventDefault(); show(cur() - 1); }
    else if (/^[1-9]$/.test(k) && +k <= screens.length) show(+k - 1);
    else if (k === 'r' || k === 'R') { S.paused = !S.paused; S.shownAt = performance.now(); updateState(); wake(); }
    else if (k === 'f' || k === 'F') {
      if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.();
    }
  });

  async function start() {
    let seconds = 300;
    try { seconds = (await (await fetch('config.json')).json()).interval ?? seconds; } catch { /* keep default */ }
    if (qs.has('interval')) seconds = Number(qs.get('interval'));
    S.intervalMs = Number.isFinite(seconds) ? Math.max(0, seconds) * 1000 : 300000;
    updateState();
    show(S.idx, true);
    requestAnimationFrame(tick);
  }
  start();
})();
