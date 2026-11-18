/* Rack Dashboard - compute screen. Reads GET /api/metrics (latest sample + ~1 h history per host). */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 5000;
  let data = null, fetchedAt = 0;

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const OS_TEXT = { windows: 'Windows', linux: 'Linux', freebsd: 'FreeBSD' };
  const n = v => (typeof v === 'number' && isFinite(v) ? v : null);

  function bytes(b) {
    b = n(b); if (b == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b >= 100 || i === 0 ? Math.round(b) : b.toFixed(1)} ${u[i]}`;
  }
  function bits(Bps) {           // bytes/s -> b/s with SI units, like link speeds
    let b = (n(Bps) ?? 0) * 8;
    const u = ['b/s', 'kb/s', 'Mb/s', 'Gb/s']; let i = 0;
    while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
    return `${b >= 100 || i === 0 ? Math.round(b) : b.toFixed(1)} ${u[i]}`;
  }
  function rate(Bps) {
    let b = n(Bps) ?? 0;
    const u = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b >= 100 || i === 0 ? Math.round(b) : b.toFixed(1)} ${u[i]}`;
  }
  function uptime(s) {
    s = n(s); if (s == null) return '';
    if (s < 3600) return `up ${Math.round(s / 60)} min`;
    if (s < 172800) return `up ${Math.round(s / 3600)} h`;
    return `up ${Math.round(s / 86400)} d`;
  }
  function ago(s) {
    if (s < 90) return 'just now';
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    return `${Math.round(s / 3600)} h ago`;
  }
  const speed = m => !m ? '' : m >= 1000 ? `${+(m / 1000).toFixed(1)}G` : `${m}M`;
  const lvl = (v, warn, crit) => v == null ? '' : v >= crit ? 'lv-crit' : v >= warn ? 'lv-warn' : '';

  // Sparkline: one or more series over the same time axis. Fixed 0-100 for percentages,
  // otherwise scaled to the series' own max (with a floor so idle links don't look busy).
  function spark(history, keys, { max = null, floor = 1, cls = '' } = {}) {
    const W = 220, H = 46, pts = history.length;
    if (pts < 2) return `<svg class="spark ${cls}" viewBox="0 0 ${W} ${H}"></svg>`;
    const top = max ?? Math.max(floor, ...history.flatMap(p => keys.map(k => n(p[k]) ?? 0)));
    const lines = keys.map((k, j) => {
      const xy = history.map((p, i) => `${(i / (pts - 1) * W).toFixed(1)},${(H - 1 - (Math.min(n(p[k]) ?? 0, top) / top) * (H - 3)).toFixed(1)}`);
      return `${j === 0 ? `<polygon class="area" points="0,${H} ${xy.join(' ')} ${W},${H}"/>` : ''}<polyline class="s${j}" points="${xy.join(' ')}"/>`;
    }).join('');
    return `<svg class="spark ${cls}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${lines}</svg>`;
  }

  // Per-core strip: one cell per logical CPU, shaded by utilisation.
  function cores(per) {
    if (!per?.length) return '';
    const cell = v => `<i style="--u:${(Math.min(100, n(v) ?? 0) / 100).toFixed(2)}" title="${Math.round(n(v) ?? 0)}%"></i>`;
    return `<div class="cores" style="--cols:${Math.min(per.length, 40)}">${per.map(cell).join('')}</div>`;
  }

  function cpuBlock(h) {
    const c = h.cpu || {}, util = n(c.util_pct);
    const facts = [
      c.load ? `load ${c.load.map(x => x.toFixed(2)).join(' ')}` : null,
      n(c.temp_c) != null ? `<span class="${lvl(c.temp_c, 85, 95)}">${Math.round(c.temp_c)}°C</span>` : null,
      c.threads ? `${c.sockets > 1 ? `${c.sockets}× ` : ''}${c.cores}C/${c.threads}T` : null,
    ].filter(Boolean).join(' · ');
    return `<div class="blk cpu">
      <div class="lab">CPU</div>
      <div class="big ${lvl(util, 95, 101)}">${util == null ? '—' : Math.round(util)}<small>%</small></div>
      <div class="facts">${facts}</div>
      ${spark(h.history, ['cpu'], { max: 100, cls: 'c-cpu' })}
      ${cores(c.per_core_pct)}
    </div>`;
  }

  function memBlock(h) {
    const m = h.mem || {}, pct = m.total ? 100 * m.used / m.total : null;
    const extra = [
      m.swap_total ? `swap ${bytes(m.swap_used)} / ${bytes(m.swap_total)}` : null,
      n(m.commit_pct) != null ? `commit ${Math.round(m.commit_pct)}%` : null,
    ].filter(Boolean).join(' · ');
    return `<div class="blk mem">
      <div class="lab">Memory</div>
      <div class="big ${lvl(pct, 90, 97)}">${pct == null ? '—' : Math.round(pct)}<small>%</small></div>
      <div class="facts">${bytes(m.used)} / ${bytes(m.total)}${extra ? ` · ${extra}` : ''}</div>
      ${spark(h.history, ['mem'], { max: 100, cls: 'c-mem' })}
      <div class="bar"><b style="width:${Math.min(100, pct ?? 0).toFixed(1)}%"></b></div>
    </div>`;
  }

  function gpuBlock(h) {
    if (!h.gpus?.length) return '';
    return h.gpus.map(g => {
      const vram = g.mem_total ? 100 * g.mem_used / g.mem_total : null;
      const facts = [
        n(g.temp_c) != null ? `<span class="${lvl(g.temp_c, 90, 95)}">${Math.round(g.temp_c)}°C</span>` : null,
        n(g.power_w) != null ? `${Math.round(g.power_w)} W` : null,
        n(g.enc_pct) != null ? `enc ${Math.round(g.enc_pct)}%` : null,
        n(g.dec_pct) != null ? `dec ${Math.round(g.dec_pct)}%` : null,
      ].filter(Boolean).join(' · ');
      return `<div class="blk gpu">
        <div class="lab">GPU <span class="dim">${esc(g.name)}</span></div>
        <div class="big">${n(g.util_pct) == null ? '—' : Math.round(g.util_pct)}<small>%</small></div>
        <div class="facts">${facts}</div>
        ${spark(h.history, ['gpu'], { max: 100, cls: 'c-gpu' })}
        <div class="facts">VRAM ${bytes(g.mem_used)} / ${bytes(g.mem_total)}</div>
        <div class="bar"><b style="width:${Math.min(100, vram ?? 0).toFixed(1)}%"></b></div>
      </div>`;
    }).join('');
  }

  function netBlock(h) {
    const nics = (h.net || []).slice().sort((a, b) => (b.rx_bps + b.tx_bps) - (a.rx_bps + a.tx_bps)).slice(0, 3);
    if (!nics.length) return '';
    const rows = nics.map(x => `<div class="kv"><span class="k">${esc(x.name)}${x.speed_mbps ? ` <em>${speed(x.speed_mbps)}</em>` : ''}</span>
      <span class="v">↓ ${bits(x.rx_bps)} &nbsp;↑ ${bits(x.tx_bps)}</span></div>`).join('');
    return `<div class="blk net">
      <div class="lab">Network <span class="legend"><i class="l0"></i>in <i class="l1"></i>out</span></div>
      ${spark(h.history, ['rx', 'tx'], { floor: 125000, cls: 'c-net' })}
      ${rows}
    </div>`;
  }

  function diskBlock(h) {
    const disks = h.disks || [];
    if (!disks.length) return '';
    const rd = disks.reduce((a, d) => a + (n(d.read_bps) ?? 0), 0), wr = disks.reduce((a, d) => a + (n(d.write_bps) ?? 0), 0);
    const busiest = disks.reduce((a, d) => ((n(d.busy_pct) ?? 0) > (n(a?.busy_pct) ?? -1) ? d : a), null);
    return `<div class="blk disk">
      <div class="lab">Disk I/O <span class="legend"><i class="l0"></i>read <i class="l1"></i>write</span></div>
      ${spark(h.history, ['rd', 'wr'], { floor: 1048576, cls: 'c-disk' })}
      <div class="kv"><span class="k">total</span><span class="v">R ${rate(rd)} &nbsp;W ${rate(wr)}</span></div>
      ${busiest ? `<div class="kv"><span class="k">busiest: ${esc(busiest.name)}</span><span class="v">${Math.round(n(busiest.busy_pct) ?? 0)}% busy</span></div>` : ''}
    </div>`;
  }

  function procBlock(h) {
    const p = h.procs || {};
    if (!p.top?.length) return '';
    return `<div class="blk procs">
      <div class="lab">Top processes · CPU <span class="dim">${p.count ? `of ${p.count}` : ''}</span></div>
      ${p.top.map(t => `<div class="kv"><span class="k">${esc(t.name)}</span><span class="v">${(n(t.cpu_pct) ?? 0).toFixed(1)}%</span></div>`).join('')}
    </div>`;
  }

  // Memory by program (all of a program's processes together, e.g. "svchost ×71").
  // Windows: private working set; Linux/FreeBSD: resident memory.
  function topMemBlock(h) {
    const rows = (h.procs || {}).top_mem || [];
    if (!rows.length) return '';
    const total = n(h.mem?.total);
    return `<div class="blk procs">
      <div class="lab">Top processes · memory</div>
      ${rows.map(t => `<div class="kv"><span class="k">${esc(t.name)}${t.n > 1 ? ` <span class="dim">×${t.n}</span>` : ''}</span>` +
        `<span class="v">${bytes(n(t.mem) ?? 0)}${total ? ` <span class="dim">${(100 * (n(t.mem) ?? 0) / total).toFixed(0)}%</span>` : ''}</span></div>`).join('')}
    </div>`;
  }

  function card(h, extraAge) {
    const age = h.age_s + extraAge, stale = h.stale || age > (data.stale_after_s || 60);
    const badges = [
      h.pending_reboot ? '<span class="tag warn">reboot pending</span>' : '',
      h.agent_status?.outdated ? `<span class="tag outdated" title="Running ${esc(h.agent)}">agent v${h.agent_status.version} · update available</span>` : '',
    ].join('');
    const reasons = h.reasons?.length ? `<div class="creasons">${h.reasons.map(r => `<span class="lv-${r.level}">▲ ${esc(r.text)}</span>`).join('')}</div>` : '';
    return `<article class="chost lv-${h.health}${stale ? ' stale' : ''}">
      <header>
        <span class="sd big"></span>
        <h2>${esc(h.host)}</h2>
        <span class="os">${esc(OS_TEXT[h.os] || h.os || '')}</span>
        <span class="model">${esc(h.cpu?.model || '')}</span>
        ${badges}
        <span class="when ${stale ? 'lv-warn' : ''}">${stale ? `STALE · ${ago(age)}` : uptime(h.uptime_s)}</span>
      </header>
      ${reasons}
      <div class="cgrid${h.gpus?.length ? ' g4' : ''}">
        ${cpuBlock(h)}${memBlock(h)}${gpuBlock(h)}${netBlock(h)}${diskBlock(h)}${procBlock(h)}${topMemBlock(h)}
      </div>
    </article>`;
  }

  function render() {
    if (!data) return;
    const extraAge = Math.round((Date.now() - fetchedAt) / 1000);
    const hosts = data.hosts || [];
    $('computeEmpty').hidden = hosts.length > 0;
    $('computeHosts').innerHTML = hosts.map(h => card(h, extraAge)).join('');
    const issues = hosts.filter(h => h.health !== 'ok').length;
    const stale = hosts.filter(h => h.stale || h.age_s + extraAge > (data.stale_after_s || 60)).length;
    const reboots = hosts.filter(h => h.pending_reboot).length;
    $('computeSummary').innerHTML = `<span class="chip">${hosts.length} hosts</span>` +
      (issues ? `<span class="chip lv-warn">${issues} need attention</span>` : `<span class="chip lv-ok">all normal</span>`) +
      (reboots ? `<span class="chip lv-warn">${reboots} reboot pending</span>` : '') +
      (stale ? `<span class="chip lv-warn">${stale} stale</span>` : '');
  }

  async function refresh() {
    try {
      const r = await fetch('api/metrics', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
      fetchedAt = Date.now();
    } catch (e) {
      console.warn('metrics: fetch failed', e);
    }
    render();
  }

  document.addEventListener('screenchange', e => { if (e.detail.id === 'compute') refresh(); });
  setInterval(refresh, REFRESH_MS);
  refresh();
})();
