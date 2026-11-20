/* Rack Dashboard - Attack patterns screen. Reads GET /api/honeypot/patterns: honeypot shell sessions
 * over 30 days, grouped by what they ran (normalized command sequence = one pattern).
 * Attacker-typed text is escaped everywhere; payload hosts are shown as text, never links.
 * The spotlight rotates through the top patterns; clicking a pattern (table or "new" list) pins it,
 * ↑/↓ step through the table, Esc / clicking it again / ▶ resumes. A pin ends by itself after
 * PIN_MS without interaction, so a wall display always goes back to rotating. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = v => (typeof v === 'number' && isFinite(v) ? v.toLocaleString('en-US') : '—');
  const flag = cc => (cc && /^[A-Za-z]{2}$/.test(cc)) ? String.fromCodePoint(...[...cc.toUpperCase()].map(c => 127397 + c.charCodeAt(0))) : '🏳';
  const ACTIVE_MS = 15000, IDLE_MS = 60000, SPOT_MS = 12000, SPOT_N = 8, PIN_MS = 10 * 60000;
  let data = null, fetchedAt = 0, active = false, timer = null, spot = 0;
  let pinned = null, touchedAt = 0, shownFp = null;   // pinned pattern (fingerprint), last interaction

  // Keep this screen up while someone is using it (screens.js restarts its rotation countdown).
  const hold = () => { touchedAt = Date.now(); document.dispatchEvent(new Event('screenhold')); };
  const all = () => [...(data?.patterns || []), ...(data?.new || [])];
  function current() {
    if (pinned) {
      const p = all().find(x => x.fp === pinned);
      if (p) return p;
      pinned = null;                                       // aged out of the data: back to rotating
    }
    const top = (data?.patterns || []).slice(0, SPOT_N);
    return top.length ? top[spot % top.length] : null;
  }
  function pin(fp) {
    pinned = pinned === fp ? null : fp;
    hold(); table(); spotlight();
  }

  const since = () => (Date.now() - fetchedAt) / 1000;
  function ago(s) {
    if (typeof s !== 'number') return '—';
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
    if (s < 172800) return `${Math.round(s / 3600)} h`;
    return `${Math.round(s / 86400)} d`;
  }
  const tags = t => (t || []).filter(x => x !== 'liveness').map(x => `<i class="tag t-${esc(x)}">${esc(x)}</i>`).join('');
  const SPREAD = { botnet: 'lv-crit', 'small group': 'lv-warn', 'single source': 'lv-unknown' };

  function spark(daily) {
    const d = daily || [], max = Math.max(1, ...d), W = 84, H = 18, bw = W / Math.max(1, d.length);
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">` + d.map((v, i) => {
      const h = Math.max(v ? 1.5 : 0.6, H * v / max);
      return `<rect x="${(i * bw + 0.5).toFixed(1)}" y="${(H - h).toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${h.toFixed(1)}"${i === d.length - 1 ? ' class="today"' : ''}/>`;
    }).join('') + '</svg>';
  }

  function table() {
    const ps = data.patterns || [];
    const cur = current();
    $('patRows').innerHTML = ps.map((p, i) => `
      <tr data-fp="${esc(p.fp)}" class="${cur && p.fp === cur.fp ? 'on' : ''}${pinned === p.fp ? ' pinned' : ''}" title="${pinned === p.fp ? 'Pinned: click again to resume rotation' : 'Click to pin in the spotlight'}">
        <td class="rk">${i + 1}</td>
        <td class="lbl"><b>${esc(p.label)}</b>${p.first_age_s + since() < 86400 ? ' <span class="pnew">new</span>' : ''}<div class="tags">${tags(p.tags)}<code class="peek">${esc(p.script.slice(0, 3).join('; '))}</code></div></td>
        <td class="n">${num(p.n_24h)}</td><td class="n dim">${num(p.n_30d)}</td>
        <td class="n">${num(p.ips)}</td><td class="n">${num(p.countries)}</td>
        <td><span class="chip ${SPREAD[p.spread] || ''}">${esc(p.spread)}</span></td>
        <td class="n dim">${ago(p.first_age_s + since())}</td><td class="n dim">${ago(p.last_age_s + since())}</td>
        <td>${spark(p.daily)}</td>
      </tr>`).join('') || '<tr><td colspan="10" class="none">No shell sessions recorded yet.</td></tr>';
  }

  function spotlight() {
    const top = (data.patterns || []).slice(0, SPOT_N);
    const p = current();
    const box = $('patSpot');
    if (!p) { box.innerHTML = '<p class="none">Nothing to show yet.</p>'; shownFp = null; return; }
    // Same pattern as before (a data refresh): keep the reader's scroll positions.
    const keep = p.fp === shownFp ? ['', 'pre', '.srcs'].map(s => (s ? box.querySelector(s) : box)?.scrollTop || 0) : null;
    const head = pinned
      ? `<h2>Pinned <span class="muted">· Esc or click it again to resume</span></h2><button class="resume" title="Resume rotation (Esc)">▶ rotate</button>`
      : `<h2>Spotlight <span class="muted">· ${spot % Math.max(1, top.length) + 1} of ${top.length} · click a pattern to pin it</span></h2>`;
    const src = p.sources || [];
    box.innerHTML = `
      <div class="spot-head">${head}<code class="fp">${esc(p.fp)}</code></div>
      <h3>${esc(p.label)}</h3>
      <div class="tags">${tags(p.tags)}<span class="chip ${SPREAD[p.spread] || ''}">${esc(p.spread)}</span></div>
      <p class="sum">${esc(p.summary)}</p>
      <div class="facts">
        <div><span class="k">Last 24 h</span><span class="v">${num(p.n_24h)}</span></div>
        <div><span class="k">30 days</span><span class="v">${num(p.n_30d)}</span></div>
        <div><span class="k">Source IPs</span><span class="v">${num(p.ips)}</span></div>
        <div><span class="k">First seen</span><span class="v">${ago(p.first_age_s + since())} ago</span></div>
      </div>
      <h4>The script <span class="muted">· ${num(p.script.length)} command${p.script.length === 1 ? '' : 's'} · normalized: &lt;url&gt;, &lt;file&gt;, &lt;ssh-key&gt; vary per run</span></h4>
      <pre>${p.script.map(c => `<span class="ps">$</span> ${esc(c)}`).join('\n')}</pre>
      <div class="cols">
        <div><h4>Got in with</h4>${p.creds.map(c => `<div class="row"><code>${esc(c[0])}</code><span>${num(c[1])}</span></div>`).join('')}</div>
        <div><h4>Source IPs <span class="muted">· ${num(p.ips)} · ${p.top_countries.map(c => `<span title="${esc(c[1])}">${flag(c[0])} ${num(c[2])}</span>`).join(' ')}</span></h4>
          <div class="srcs">${src.map(s => `<div class="row" title="${esc([s[2] || s[1], `${num(s[3])} session${s[3] === 1 ? '' : 's'}`, `last ${ago(s[4] + since())} ago`].filter(Boolean).join(' · '))}">` +
            `<span>${flag(s[1])} <code>${esc(s[0])}</code></span><span>${num(s[3])}×</span></div>`).join('') || '<div class="row dim">—</div>'}</div>
          ${p.ips > src.length ? `<div class="more">busiest ${src.length} of ${num(p.ips)} shown</div>` : ''}</div>
        <div><h4>Payload hosts <span class="muted">· blocked</span></h4>${p.hosts.length ? p.hosts.map(h => `<div class="row"><code class="bad">${esc(h[0])}</code><span>${num(h[1])}</span></div>`).join('') : '<div class="row dim">none</div>'}
          ${(p.uploads || []).length ? `<h4>Uploaded files <span class="muted">· captured · SHA-256</span></h4>` + p.uploads.map(u => `<div class="row" title="${esc(u[0])}"><span><code class="bad">${esc(u[0].slice(0, 16))}…</code> <span class="dim">${esc(u[1])}</span></span><span>${num(u[2])}</span></div>`).join('') : ''}</div>
      </div>`;
    if (keep) ['', 'pre', '.srcs'].forEach((s, i) => { const el = s ? box.querySelector(s) : box; if (el) el.scrollTop = keep[i]; });
    shownFp = p.fp;
  }

  function lists() {
    $('patNew').innerHTML = (data.new || []).map(p => `
      <li data-fp="${esc(p.fp)}" class="${pinned === p.fp ? 'pinned' : ''}" title="Click to pin in the spotlight"><span class="when">${ago(p.first_age_s + since())} ago</span><span class="lbl">${esc(p.label)} <span class="tags">${tags(p.tags)}</span></span>
        <span class="n">${num(p.n_24h)}× · ${num(p.ips)} IP${p.ips === 1 ? '' : 's'}</span></li>`).join('') || '<li class="none">No new patterns in the last 24 h.</li>';
    const un = data.unrecognized || [];
    const max = un.length ? un[0][1] : 1;
    $('patUnrec').innerHTML = un.map(u => `<li title="${esc(u[0])}"><div class="fill" style="width:${(u[1] / max * 100).toFixed(1)}%"></div>` +
      `<span class="lbl"><code>${esc(u[0])}</code></span><span class="n">${num(u[1])}</span></li>`).join('') || '<li class="none">Every command matched a rule.</li>';
  }

  function render() {
    if (!data) return;
    $('patSummary').innerHTML = (data.simulated ? '<span class="chip lv-warn">simulated data</span>' : '') +
      `<span class="chip">${num(data.pattern_count)} patterns · ${data.window_days} days</span>` +
      `<span class="chip">${num(data.shell_sessions)} shell sessions</span>` +
      (data.new_24h ? `<span class="chip lv-warn">${num(data.new_24h)} new in 24 h</span>` : '');
    table(); spotlight(); lists();
  }

  async function refresh() {
    try {
      const r = await fetch('api/honeypot/patterns', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
      fetchedAt = Date.now();
    } catch (e) {
      console.warn('patterns: fetch failed', e);
    }
    render();
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(refresh, active ? ACTIVE_MS : IDLE_MS);
  }
  setInterval(() => {
    if (!active || !data) return;
    if (pinned && Date.now() - touchedAt > PIN_MS) pinned = null;   // nobody's looking any more
    if (!pinned) spot++;
    table(); spotlight(); lists();
  }, SPOT_MS);

  // ---------- interaction ----------
  $('patRows').addEventListener('click', e => { const tr = e.target.closest('tr[data-fp]'); if (tr) pin(tr.dataset.fp); });
  $('patNew').addEventListener('click', e => { const li = e.target.closest('li[data-fp]'); if (li) { pin(li.dataset.fp); lists(); } });
  $('patSpot').addEventListener('click', e => { if (e.target.closest('.resume')) { pinned = null; hold(); table(); spotlight(); lists(); } });
  $('patterns').addEventListener('wheel', hold, { passive: true });
  document.addEventListener('keydown', e => {
    if (!active || !data || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === 'Escape' && pinned) { pinned = null; hold(); table(); spotlight(); lists(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const ps = data.patterns || [];
      if (!ps.length) return;
      e.preventDefault();
      const i = ps.findIndex(p => p.fp === current()?.fp);
      const next = ps[Math.min(ps.length - 1, Math.max(0, (i < 0 ? 0 : i) + (e.key === 'ArrowDown' ? 1 : -1)))];
      pinned = null; pin(next.fp); lists();
      $('patRows').querySelector('tr.pinned')?.scrollIntoView({ block: 'nearest' });
    }
  });
  document.addEventListener('screenchange', e => {
    active = e.detail.id === 'patterns';
    if (active) { spot = 0; pinned = null; refresh(); }
    schedule();
  });
  refresh();
  schedule();
})();
