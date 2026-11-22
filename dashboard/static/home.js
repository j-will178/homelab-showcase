/* Home page (/home/): section tiles with a live line or two each. Plain JS, no build step. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const li = (cls, text) => `<li class="${cls}">${esc(text)}</li>`;
  const getJson = async url => { const r = await fetch(url, { cache: 'no-store' }); if (!r.ok) throw new Error(r.status); return r.json(); };

  async function rack() {
    const out = [];
    try {
      const d = await getJson('api/smart');
      const hosts = d.hosts || [];
      const worst = ['crit', 'warn'].find(l => hosts.some(h => h.health === l)) || 'ok';
      const stale = hosts.filter(h => h.stale || h.age_s > (d.stale_after_s || 2700)).length;
      const bad = hosts.filter(h => h.health === worst).length;
      out.push(li(worst === 'crit' ? 'crit' : stale ? 'warn' : worst, `${hosts.length} hosts · ` +
        (worst === 'ok' ? 'drives healthy' : `${bad} with ${worst === 'crit' ? 'critical' : 'warning'} drives`) +
        (stale ? ` · ${stale} stale` : '')));
    } catch { out.push(li('dim', 'drive health unavailable')); }
    try {
      const h = await getJson('api/honeypot');
      const s = h.summary || {};
      if (h.source === 'waiting') out.push(li('dim', 'honeypot · waiting for the first events'));
      else if (h.source) out.push(li(h.source === 'cowrie' ? 'ok' : 'dim',
        `honeypot · ${(s.sessions || 0).toLocaleString()} sessions in 24 h${h.source === 'cowrie' ? '' : ' (simulated)'}`));
    } catch { /* honeypot off: say nothing */ }
    $('rackLive').innerHTML = out.join('');
  }

  function clock() {
    $('homeClock').textContent = new Date().toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const tile = document.querySelector(`a.tile[data-key="${e.key}"]`);
    if (tile) location.href = tile.getAttribute('href');
  });

  clock(); setInterval(clock, 15000);
  rack(); setInterval(rack, 60000);
})();
