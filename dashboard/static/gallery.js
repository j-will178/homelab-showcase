/* Space gallery (/space/gallery/): cycles through the newest Webb and Hubble release images from
 * api/space/gallery, one every 20 s. ←/→ browse, Space pauses, S goes to Space, Home to the home page.
 * The list refreshes every 30 minutes (the server checks the ESA feeds every 6 h).
 * Images fill the screen (object-fit: cover). When an image's shape is far from the screen's, a fixed crop
 * would lose a lot of it, so the crop pans slowly from one edge to the other while it's shown: top to bottom
 * for tall images, left to right for very wide ones. Pausing freezes the pan where it is. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const SHOW_MS = 20000, REFRESH_MS = 30 * 60000;
  const COLOR = { Webb: '#a78bfa', Hubble: '#2dd4bf' };
  const PAN_RATIO = 1.25;   // pan when image and screen aspect ratios differ by more than this
  const still = matchMedia('(prefers-reduced-motion: reduce)');
  let items = [], i = 0, paused = false, timer = null;

  const day = iso => iso ? new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' }) : '';

  function show(k) {
    if (!items.length) return;
    i = (k + items.length) % items.length;
    const it = items[i];
    document.body.style.setProperty('--tc', COLOR[it.telescope] || 'var(--accent)');
    const img = $('gImg');
    img.classList.remove('on');
    if (it.image) {
      const next = new Image();
      next.onload = () => { img.src = it.image; img.alt = it.title; pan(img, next.naturalWidth, next.naturalHeight); img.classList.add('on'); };
      next.src = it.image;
      $('gEmpty').hidden = true;
    } else {
      img.removeAttribute('src');
      $('gEmpty').hidden = false;
      $('gEmpty').textContent = 'No image file for this entry yet';
    }
    $('gTel').textContent = it.telescope;
    $('gKind').textContent = it.kind;
    $('gDate').textContent = day(it.date);
    $('gCount').textContent = `${i + 1} / ${items.length}${paused ? ' · paused' : ''}`;
    $('gTitle').textContent = it.title;
    $('gSum').textContent = it.summary || '';
    $('gCredit').textContent = it.credit || '';
    const a = $('gLink');
    if (it.url) { a.href = it.url; a.hidden = false; } else a.hidden = true;
    [...$('gDots').children].forEach((b, n) => b.classList.toggle('on', n === i));
    restart();
  }

  // Start position, end position and duration for the crop, from the image and screen shapes.
  function pan(img, w, h) {
    const ratio = (w / h) / (innerWidth / innerHeight);
    let from = '50% 50%', to = null;
    if (ratio < 1 / PAN_RATIO) { from = '50% 0%'; to = '50% 100%'; }        // tall: top to bottom
    else if (ratio > PAN_RATIO) { from = '0% 50%'; to = '100% 50%'; }       // very wide: left to right
    img.style.transition = 'opacity .8s ease';
    img.style.objectPosition = from;
    img.dataset.to = (to && !still.matches) ? to : '';
    if (!paused) startPan(img);
  }

  function startPan(img) {
    if (!img.dataset.to) return;
    void img.offsetWidth;                                                   // commit the start position first
    img.style.transition = `opacity .8s ease, object-position ${SHOW_MS - 1000}ms linear`;
    img.style.objectPosition = img.dataset.to;
  }

  function freezePan(img) {
    const at = getComputedStyle(img).objectPosition;
    img.style.transition = 'opacity .8s ease';
    img.style.objectPosition = at;
  }

  function restart() {
    clearTimeout(timer);
    if (!paused && items.length > 1) timer = setTimeout(() => show(i + 1), SHOW_MS);
  }

  async function load() {
    try {
      const r = await fetch('api/space/gallery', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      const cur = items[i]?.id;
      items = d.items || [];
      $('gDots').innerHTML = items.map((it, n) => `<button aria-label="Image ${n + 1}" data-n="${n}"></button>`).join('');
      if (!items.length) {
        $('gEmpty').hidden = false;
        $('gEmpty').textContent = d.error ? 'Release images unavailable right now' : 'Loading the latest release images…';
        return;
      }
      const keep = items.findIndex(it => it.id === cur);
      show(keep >= 0 ? keep : 0);
    } catch (e) {
      console.warn('gallery: fetch failed', e);
      if (!items.length) { $('gEmpty').hidden = false; $('gEmpty').textContent = 'Release images unavailable right now'; }
    }
  }

  $('gDots').addEventListener('click', e => { const b = e.target.closest('button[data-n]'); if (b) show(+b.dataset.n); });
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === 'ArrowRight' || e.key === 'PageDown') show(i + 1);
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') show(i - 1);
    else if (e.key === ' ') {
      e.preventDefault(); paused = !paused;
      if (paused) freezePan($('gImg')); else startPan($('gImg'));
      $('gCount').textContent = `${i + 1} / ${items.length}${paused ? ' · paused' : ''}`;
      restart();
    }
    else if (e.key === 's' || e.key === 'S') location.href = 'space/';
    else if (e.key === 'Home') location.href = 'home/';
  });
  load();
  setInterval(load, REFRESH_MS);
})();
