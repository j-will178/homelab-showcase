/* Rack Dashboard - drive health (SMART) screen. Reads GET /api/smart. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 30000;
  let data = null, fetchedAt = 0;

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const LEVEL_TEXT = { ok: 'OK', warn: 'Warning', crit: 'Critical', unknown: 'Unknown' };
  const OS_TEXT = { windows: 'Windows', linux: 'Linux', freebsd: 'FreeBSD' };

  function bytes(n) {
    if (!n) return '—';
    const tb = n / 1e12;
    return tb >= 1 ? `${+tb.toFixed(tb >= 10 ? 0 : 1)} TB` : `${Math.round(n / 1e9)} GB`;
  }
  function hours(h) {
    if (h == null) return '—';
    if (h < 48) return `${h} h`;
    const d = h / 24;
    return d < 365 ? `${Math.round(d)} d` : `${(d / 365).toFixed(1)} y`;
  }
  function ago(s) {
    if (s < 90) return 'just now';
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 172800) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  // Limits come from the server: the drive's own rated max when it reports one.
  function tempClass(d) {
    const lim = d.temp_limits || [50, 58];
    return d.temp_c == null ? '' : d.temp_c >= lim[1] ? 'lv-crit' : d.temp_c >= lim[0] ? 'lv-warn' : '';
  }

  // The one or two numbers that matter most for this kind of drive.
  function metric(d) {
    const c = d.counters || {}, parts = [];
    if (d.wear_pct != null) parts.push(`wear ${d.wear_pct}%`);
    if (d.bus === 'SAS') {
      if (c.grown_defects != null) parts.push(`defects ${c.grown_defects}`);
      if (c.uncorrected_errors != null) parts.push(`unc ${c.uncorrected_errors}`);
    } else if (d.kind === 'nvme') {
      if (c.media_errors != null) parts.push(`media err ${c.media_errors}`);
    } else if (d.source === 'windows') {
      const unc = (c.read_errors_uncorrected || 0) + (c.write_errors_uncorrected || 0);
      parts.push(`unc ${unc}`);
    } else {
      if (c.reallocated != null) parts.push(`realloc ${c.reallocated}`);
      if (c.pending != null) parts.push(`pend ${c.pending}`);
    }
    return parts.join(' · ') || '—';
  }

  function driveRow(d) {
    const kind = { hdd: 'HDD', ssd: 'SSD', nvme: 'NVMe' }[d.kind] || '?';
    const busTag = d.kind === 'nvme' ? '' : ` · ${esc(d.bus)}`;
    const reasons = d.health !== 'ok' && d.reasons?.length
      ? `<div class="reasons">${d.reasons.map(r => `<span class="lv-${r.level}">${esc(r.text)}</span>`).join('')}</div>` : '';
    return `<div class="drive lv-${d.health}">
      <span class="sd" title="${LEVEL_TEXT[d.health]}"></span>
      <div class="dname"><b>${esc(d.model)}</b><span class="mono dim">${esc(d.serial || '')}${d.device ? ` · ${esc(d.device)}` : ''}${d.source === 'windows' ? ' · via Windows' : ''}</span></div>
      <span class="tags"><span class="tag">${kind}${busTag}</span>${d.pool ? `<span class="tag pool">${esc(d.pool)}</span>` : ''}</span>
      <span class="mono num">${bytes(d.capacity_bytes)}</span>
      <span class="mono num ${tempClass(d)}">${d.temp_c != null ? `${d.temp_c}°` : '—'}</span>
      <span class="mono num">${hours(d.power_on_hours)}</span>
      <span class="mono metric">${metric(d)}</span>
      ${reasons}
      ${d.notes?.length ? `<div class="notes">${d.notes.map(n => `<span>${esc(n)}</span>`).join('')}</div>` : ''}
    </div>`;
  }

  function hostCard(h, extraAge) {
    const age = h.age_s + extraAge;
    const stale = h.stale || age > (data.stale_after_s || 2700);
    const s = h.summary, issues = s.crit + s.warn + s.unknown;
    const counts = issues ? ['crit', 'warn', 'unknown'].filter(l => s[l]).map(l => `<span class="lv-${l}">${s[l]} ${LEVEL_TEXT[l].toLowerCase()}</span>`).join(' · ') : 'all healthy';
    const errors = h.errors?.length ? `<div class="host-errors">${h.errors.map(e => `<div>agent note: ${esc(e)}</div>`).join('')}</div>` : '';
    return `<article class="host lv-${h.health}${stale ? ' stale' : ''}">
      <header>
        <span class="sd big"></span>
        <h2>${esc(h.host)}</h2>
        <span class="os">${esc(OS_TEXT[h.os] || h.os || '')}</span>
        ${h.agent_status?.outdated ? `<span class="tag outdated" title="Running ${esc(h.agent)}; repo has v${h.agent_status.latest}">agent v${h.agent_status.version} · update available</span>` : ''}
        <span class="count">${h.drives.length} drive${h.drives.length === 1 ? '' : 's'} · ${counts}</span>
        <span class="when ${stale ? 'lv-warn' : ''}">${stale ? 'STALE · ' : ''}${ago(age)}</span>
      </header>
      ${errors}
      <div class="drives">
        <div class="drive head"><span></span><span>Drive</span><span>Type</span><span>Size</span><span>Temp</span><span>On</span><span>Health</span></div>
        ${h.drives.map(driveRow).join('')}
      </div>
    </article>`;
  }

  function render() {
    if (!data) return;
    const extraAge = Math.round((Date.now() - fetchedAt) / 1000);
    const hosts = data.hosts || [];
    $('smartEmpty').hidden = hosts.length > 0;
    $('smartHosts').innerHTML = hosts.map(h => hostCard(h, extraAge)).join('');

    const tot = { ok: 0, warn: 0, crit: 0, unknown: 0 };
    let drives = 0, stale = 0;
    hosts.forEach(h => {
      drives += h.drives.length;
      Object.keys(tot).forEach(k => { tot[k] += h.summary[k] || 0; });
      if (h.stale || h.age_s + extraAge > (data.stale_after_s || 2700)) stale++;
    });
    $('smartSummary').innerHTML =
      `<span class="chip">${hosts.length} hosts · ${drives} drives</span>` +
      `<span class="chip lv-ok">${tot.ok} ok</span>` +
      (tot.warn ? `<span class="chip lv-warn">${tot.warn} warning</span>` : '') +
      (tot.crit ? `<span class="chip lv-crit">${tot.crit} critical</span>` : '') +
      (tot.unknown ? `<span class="chip lv-unknown">${tot.unknown} unknown</span>` : '') +
      (stale ? `<span class="chip lv-warn">${stale} stale host${stale > 1 ? 's' : ''}</span>` : '');
  }

  async function refresh() {
    try {
      const r = await fetch('api/smart', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
      fetchedAt = Date.now();
    } catch (e) {
      console.warn('smart: fetch failed', e);
    }
    render();
  }

  document.addEventListener('screenchange', e => { if (e.detail.id === 'smart') refresh(); });
  setInterval(refresh, REFRESH_MS);
  refresh();
})();
