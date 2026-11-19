/* Rack Dashboard - Honeypot screen. Reads GET /api/honeypot (Cowrie SSH/Telnet honeypot, last 24 h).
 * Everything shown here was typed by attackers: it's all escaped, never inserted as HTML. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = v => (typeof v === 'number' && isFinite(v) ? v.toLocaleString('en-US') : '—');
  const flag = cc => (cc && /^[A-Za-z]{2}$/.test(cc)) ? String.fromCodePoint(...[...cc.toUpperCase()].map(c => 127397 + c.charCodeAt(0))) : '🏳';
  const ACTIVE_MS = 3000, IDLE_MS = 30000;
  let data = null, fetchedAt = 0, active = false, world = null, projection = null, timer = null, dotScale = 1;

  // The lab side (cloud-ops: log puller + weekly rebuild) and the honeypot's health snapshot,
  // from the puller's last post. Chips marked data-diag open the diagnostics view (also: D key).
  const MODES = { 'lab-debug': ['lv-warn', 'lab-debug: home only · diagnostics on'],
                  lab: ['lv-warn', 'lab: home only · dress rehearsal'], public: ['lv-ok', 'public'] };
  const now = () => Date.now() / 1000;
  const chip = (cls, text, diag) => `<span class="chip ${cls}"${diag ? ' data-diag="1" role="button" tabindex="0"' : ''}>${esc(text)}</span>`;

  function healthProblems(h) {
    if (!h) return [];
    if (h.missing) return [['lv-unknown', 'no health snapshot yet']];
    const p = [];
    if (h.cowrie?.active && h.cowrie.active !== 'active') p.push(['lv-crit', `Cowrie ${h.cowrie.active}${h.cowrie.restarts ? ` · ${h.cowrie.restarts} restarts` : ''}`]);
    const ports = Object.entries(h.listening || {}).filter(([, v]) => v === false).map(([k]) => k);
    if (ports.length) p.push(['lv-crit', `not listening: ${ports.join(', ')}`]);
    const redir = Object.entries(h.redirects || {}).filter(([, v]) => v === false).map(([k]) => k);
    if (redir.length) p.push(['lv-crit', `redirect missing: ${redir.join(', ')}`]);
    if (h.nftables && h.nftables !== 'active') p.push(['lv-crit', `nftables ${h.nftables}`]);
    if (h.failed_units?.length) p.push(['lv-warn', `${h.failed_units.length} failed unit${h.failed_units.length > 1 ? 's' : ''}`]);
    if (h.cloud_init && !['done', 'disabled'].includes(h.cloud_init)) p.push(['lv-warn', `cloud-init ${h.cloud_init}`]);
    if (h.disk_pct >= 85) p.push(['lv-warn', `disk ${h.disk_pct}%`]);
    return p;
  }

  function feedChips(f) {
    if (!f) return '';
    const age = f.age_s + since();
    if (age > 180) return chip('lv-crit', `cloud-ops not reporting · ${ago(age)} ago`);
    const out = [];
    const msg = { 'waiting-secrets': 'honeypot: one-time secrets not set up (sudo hp-secrets on cloud-ops)',
                  'waiting-build': 'honeypot: waiting for its first build' };
    if (msg[f.state]) out.push(chip('lv-unknown', msg[f.state]));
    else if (f.state === 'error') out.push(`<span class="chip lv-crit" title="${esc(f.error || '')}" data-diag="1" role="button" tabindex="0">pull failing: ${esc((f.error || '').slice(0, 60))}</span>`);
    else if (f.pulled_at) out.push(chip('', `pulled ${ago(Math.max(0, now() - f.pulled_at))} ago`));
    if (f.mode && MODES[f.mode]) out.push(chip(MODES[f.mode][0], MODES[f.mode][1]));
    const problems = healthProblems(f.health);
    if (problems.length) problems.forEach(([cls, t]) => out.push(chip(cls, t, true)));
    else if (f.health && f.state !== 'error') out.push(chip('lv-ok', 'Cowrie running', true));
    if (f.running) out.push(chip('lv-warn', `${f.running === 'full' ? 'rebuilding' : 'applying'}…`));
    else if (f.rebuild_result === 'failed') out.push(chip('lv-crit', 'last rebuild failed', true));
    if (f.honeypot_image) out.push(chip('', f.honeypot_image.replace('hp-honeypot-', 'image ') +
      (f.last_rebuild_at ? ` · rebuilt ${ago(Math.max(0, now() - f.last_rebuild_at))} ago` : '')));
    return out.join('');
  }

  // ---------- diagnostics view ----------
  const DEBUG_TITLES = { cowrie_journal: 'Cowrie service log', boot_errors: 'Errors since boot', cloud_init: 'cloud-init',
                         nftables: 'nftables ruleset', sockets: 'Listening sockets', failed_units: 'Failed units' };
  function diagHtml(f) {
    if (!f) return '<p class="muted">cloud-ops hasn\'t reported yet.</p>';
    const h = f.health, parts = [];
    if (f.rebuild_result === 'failed') parts.push(`<h3>Last rebuild failed</h3><p>${esc(f.rebuild_error || '')}</p>` +
      (f.rebuild_error_tail ? `<pre>${esc(f.rebuild_error_tail)}</pre>` : ''));
    if (f.state === 'error') parts.push(`<h3>Pull failing</h3><pre>${esc(f.error || '')}</pre>`);
    if (!h) parts.push('<p class="muted">No health snapshot from the honeypot.</p>');
    else {
      const yn = v => v === true ? '<span class="ok">yes</span>' : v === false ? '<span class="bad">no</span>' : '—';
      const rows = [
        ['Mode', esc(h.mode || '—')], ['Cowrie', esc(`${h.cowrie?.active || '—'} (${h.cowrie?.sub || '—'})`) + (h.cowrie?.restarts ? ` · ${h.cowrie.restarts} restarts` : '')],
        ['Listening 2222 / 2223 / 47022', ['2222', '2223', '47022'].map(p => yn(h.listening?.[p])).join(' / ')],
        ['Redirect 22 / 23', ['22', '23'].map(p => yn(h.redirects?.[p])).join(' / ')],
        ['nftables', esc(h.nftables || '—')], ['cloud-init', esc(h.cloud_init || '—')],
        ['Failed units', esc((h.failed_units || []).join(', ') || 'none')],
        ['Disk', h.disk_pct != null ? `${h.disk_pct}%` : '—'], ['Up', h.uptime_s != null ? ago(h.uptime_s) : '—'],
        ['Cowrie log', h.log_bytes != null ? `${num(h.log_bytes)} bytes` : '—'],
        ['Snapshot', h.t ? `${ago(Math.max(0, now() - h.t))} ago` : '—'],
      ];
      parts.push('<table class="facts">' + rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join('') + '</table>');
      if (h.debug) parts.push(Object.entries(DEBUG_TITLES).filter(([k]) => h.debug[k])
        .map(([k, t]) => `<h3>${esc(t)}</h3><pre>${esc(h.debug[k])}</pre>`).join(''));
      else parts.push(`<p class="muted">Full diagnostics are only collected in mode lab-debug${h.mode ? ` (now: ${esc(h.mode)})` : ''}.</p>`);
    }
    return parts.join('');
  }
  function renderDiag() {
    if ($('hpDiag').hidden) return;
    const f = data?.feed_status;
    $('hpDiagSub').textContent = f?.mode ? `· ${f.mode}` : '';
    const body = $('hpDiagBody'), keep = body.scrollTop;
    body.innerHTML = diagHtml(f);
    body.scrollTop = keep;
  }
  function openDiag(open) { $('hpDiag').hidden = !open; renderDiag(); }
  $('hpSummary').addEventListener('click', e => { if (e.target.closest('[data-diag]')) openDiag(true); });
  $('hpDiagClose').addEventListener('click', () => openDiag(false));
  $('hpDiag').addEventListener('click', e => { if (e.target.id === 'hpDiag') openDiag(false); });
  document.addEventListener('keydown', e => {
    if (!active || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === 'd' || e.key === 'D') openDiag($('hpDiag').hidden);
    else if (e.key === 'Escape') openDiag(false);
  });

  function ago(s) {
    if (typeof s !== 'number') return '—';
    if (s < 60) return `${Math.max(0, Math.round(s))}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 172800) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
  }
  const since = () => (Date.now() - fetchedAt) / 1000;
  const defang = u => String(u).replace(/^http/i, 'hxxp');   // attacker URLs: never clickable, never look clickable

  // ---------- map ----------
  async function loadWorld() {
    const topo = await (await fetch('vendor/countries-50m.json')).json();
    const countries = topojson.feature(topo, topo.objects.countries);
    world = {
      land: topojson.feature(topo, topo.objects.land),
      borders: topojson.mesh(topo, topo.objects.countries, (a, b) => a !== b),
      fit: { type: 'FeatureCollection', features: countries.features.filter(f => f.id !== '010') },
    };
  }

  function drawMap() {
    const svg = d3.select('#hpMap');
    const el = svg.node();
    if (!world || !el) return;
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    projection = d3.geoNaturalEarth1().fitExtent([[8, 8], [w - 8, h - 8]], world.fit);
    dotScale = Math.max(0.45, Math.min(1, Math.min(w / 1300, h / 400)));   // smaller map, smaller dots
    const path = d3.geoPath(projection);
    svg.attr('viewBox', `0 0 ${w} ${h}`).selectAll('*').remove();
    svg.append('path').attr('class', 'land').attr('d', path(world.land));
    svg.append('path').attr('class', 'borders').attr('d', path(world.borders));
    svg.append('g').attr('class', 'pts');
    drawPoints();
  }

  function drawPoints() {
    if (!projection || !data) return;
    const pts = data.points || [];
    const max = Math.max(1, ...pts.map(p => p.n));
    const g = d3.select('#hpMap .pts');
    g.selectAll('*').remove();
    for (const p of [...pts].reverse()) {          // biggest last = on top
      const xy = projection([p.lon, p.lat]);
      if (!xy) continue;
      const r = (2.5 + 11 * Math.sqrt(p.n / max)) * dotScale;
      const fresh = p.last_age_s + since() < 60;
      g.append('circle').attr('cx', xy[0]).attr('cy', xy[1]).attr('r', r)
        .attr('class', fresh ? 'pt fresh' : 'pt')
        .append('title').text(`${p.city || ''} ${p.cc || ''} · ${p.n} sessions`);
      if (fresh) g.append('circle').attr('cx', xy[0]).attr('cy', xy[1]).attr('r', r).attr('class', 'pulse');
    }
  }

  // ---------- panels ----------
  function tile(label, value, sub, cls = '') {
    return `<div class="hstat ${cls}"><span class="k">${label}</span><span class="v">${value}</span><span class="s">${sub}</span></div>`;
  }

  function bars(el, rows, labelFn, title) {
    const max = rows.length ? rows[0][rows[0].length - 1] : 1;
    el.innerHTML = `<h2>${title}</h2><ol class="bars">` + (rows.length ? rows.map(r => {
      const n = r[r.length - 1];
      const label = labelFn(r);
      return `<li title="${esc(label.title || label.text)}"><div class="fill" style="width:${(n / max * 100).toFixed(1)}%"></div>` +
             `<span class="lbl">${label.html || esc(label.text)}</span><span class="n">${num(n)}</span></li>`;
    }).join('') : '<li class="none">nothing yet</li>') + '</ol>';
  }

  function timeline() {
    const tl = data.timeline || {};
    const s = tl.sessions || [], sh = tl.shells || [];
    if (!s.length) { $('hpTimeline').innerHTML = ''; return; }
    const W = 1000, H = 70, bw = W / s.length, max = Math.max(1, ...s);
    let out = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
    s.forEach((v, i) => {
      const h = H * v / max, x = i * bw;
      out += `<rect class="b" x="${(x + 0.5).toFixed(1)}" y="${(H - h).toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${h.toFixed(1)}"/>`;
      const k = sh[i] || 0;
      if (k) { const hs = H * k / max; out += `<rect class="sh" x="${(x + 0.5).toFixed(1)}" y="${(H - hs).toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${hs.toFixed(1)}"/>`; }
    });
    out += '</svg>';
    const hours = Math.round((data.window_s || 86400) / 3600);
    $('hpTimeline').innerHTML = `<h2>Sessions per 15 min <span class="muted">· <i class="key sh"></i> got a (fake) shell</span></h2>${out}` +
      `<div class="axis"><span>${hours} h ago</span><span>${hours / 2} h ago</span><span>now</span></div>`;
  }

  const KIND = {
    connect: ['conn', 'connect'], client: ['client', 'client'], login_failed: ['fail', 'login'],
    login_success: ['ok', 'login ✓'], command: ['cmd', 'cmd'], download: ['dl', 'download'], closed: ['close', 'closed'],
  };

  function feed() {
    $('hpFeed').innerHTML = (data.feed || []).map(e => {
      const [cls, label] = KIND[e.kind] || ['conn', e.kind];
      return `<li><span class="t">${ago(e.age_s + since())}</span>` +
        `<span class="who" title="${esc([e.city, e.country].filter(Boolean).join(', '))}">${flag(e.cc)} ${esc(e.ip)}</span>` +
        `<span class="kind ${cls}">${label}</span><span class="txt">${esc(e.kind === 'download' ? defang(e.text) : e.text)}</span></li>`;
    }).join('') || '<li class="none">waiting for traffic</li>';
  }

  // Shell sessions: what they tried to do (rule-based summary + tags), not the raw command list.
  // The commands are still there on hover.
  const tagChips = tags => (tags || []).filter(t => t !== 'liveness').map(t => `<i class="tag t-${esc(t)}">${esc(t)}</i>`).join('');
  function patternLine(s) {
    const p = s.pattern;
    if (!p) return '<span class="pnew">new pattern</span>';
    if (p.first_age_s < 86400) return `<span class="pnew">new pattern</span> · seen ${num(p.n_30d)}× from ${num(p.ips)} IPs`;
    return `same script seen ${num(p.n_30d)}× from ${num(p.ips)} IPs · first ${ago(p.first_age_s + since())} ago`;
  }
  function shells() {
    $('hpShells').innerHTML = (data.shells || []).map(s => `
      <article class="shell" title="${esc(s.commands.map(c => '$ ' + c).join('\n'))}">
        <header><b>${esc(s.username)} / ${esc(s.password)}</b>
          <span>${flag(s.cc)} ${esc(s.city || s.ip)} · ${esc(s.protocol)} · ${ago(s.age_s + since())} ago${s.duration_s != null ? ` · ${s.duration_s}s` : s.open ? ' · still connected' : ''}</span></header>
        <div class="tags">${tagChips(s.tags)}<span class="ncmd">${s.commands.length + s.more} command${s.commands.length + s.more === 1 ? '' : 's'}</span></div>
        <p class="sum">${esc(String(s.summary || '').replace(/^Got in as .*?\. /, ''))}</p>
        <div class="pline">${patternLine(s)}</div>
      </article>`).join('') || '<p class="none">No one has got a shell yet.</p>';
    // Show only whole cards: drop the ones that would be cut off at the bottom of the panel.
    const box = $('hpShells'), limit = box.getBoundingClientRect().bottom;
    for (const card of [...box.querySelectorAll('.shell')]) if (card.getBoundingClientRect().bottom > limit + 1) card.remove();
  }

  function render() {
    if (!data) return;
    const sm = data.summary || {};
    const pct = sm.login_attempts ? (100 * sm.logins_ok / sm.login_attempts).toFixed(1) : '0';
    $('hpStats').innerHTML =
      tile('Sessions', num(sm.sessions), `${sm.sessions_per_min ?? 0} / min now`) +
      tile('Unique sources', num(sm.unique_ips), `${num(sm.countries)} countries`) +
      tile('Login attempts', num(sm.login_attempts), 'username / password tries') +
      tile('Got a shell', num(sm.logins_ok), `${pct}% of attempts · fake shell`, 'warn') +
      tile('Commands typed', num(sm.commands), 'in the fake shell') +
      tile('Download attempts', num(sm.downloads), 'all blocked (no egress)', sm.downloads ? 'crit' : '');

    const sim = data.simulated;
    const lastAge = data.last_event_age_s != null ? data.last_event_age_s + since() : null;
    const source = sim ? '<span class="chip lv-warn">simulated data</span>'
      : data.source === 'cowrie' ? '<span class="chip lv-ok">live · cowrie</span>' : '<span class="chip lv-unknown">no data yet</span>';
    $('hpSummary').innerHTML = source +
      `<span class="chip">last event ${lastAge != null ? ago(lastAge) + ' ago' : '—'}</span>` + feedChips(data.feed_status);
    renderDiag();
    $('hpSub').textContent = sim
      ? 'Cowrie SSH/Telnet honeypot · last 24 h · SIMULATED until the cloud honeypot is connected'
      : 'Cowrie SSH/Telnet honeypot in the cloud · last 24 h · pulled through the bastion';

    const top = data.top || {};
    bars($('hpUsers'), top.usernames || [], r => ({ text: r[0] || '(empty)' }), 'Usernames');
    bars($('hpPasswords'), top.passwords || [], r => ({ text: r[0] || '(empty)' }), 'Passwords');
    bars($('hpCommands'), top.commands || [], r => ({ text: r[0], html: `<code>${esc(r[0])}</code>` }), 'Commands');
    bars($('hpCountries'), top.countries || [], r => ({ text: r[1], html: `${flag(r[0])} ${esc(r[1])}` }), 'Countries');
    bars($('hpClients'), top.clients || [], r => ({ text: r[0], html: `<code>${esc(r[0])}</code>` }), 'SSH clients');
    timeline();
    feed();
    shells();
    drawPoints();
  }

  async function refresh() {
    try {
      const r = await fetch('api/honeypot', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
      fetchedAt = Date.now();
    } catch (e) {
      console.warn('honeypot: fetch failed', e);
    }
    render();
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(refresh, active ? ACTIVE_MS : IDLE_MS);
  }

  document.addEventListener('screenchange', e => {
    active = e.detail.id === 'honeypot';
    if (active) { drawMap(); refresh(); }
    schedule();
  });
  // Redraw when the map's box changes size (window resize, zoom/scaling, layout settling).
  let lastSize = '';
  new ResizeObserver(() => {
    const el = $('hpMap'), size = `${el.clientWidth}x${el.clientHeight}`;
    if (size !== lastSize) { lastSize = size; drawMap(); }
  }).observe($('hpMap'));

  loadWorld().then(() => { drawMap(); refresh(); }).catch(e => console.warn('honeypot: map', e));
  schedule();
})();
