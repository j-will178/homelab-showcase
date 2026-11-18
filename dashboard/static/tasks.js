/* Rack Dashboard - Scheduled tasks screen. Reads GET /api/tasks (tasks in \Homelab\ on each host). */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 15000;
  let data = null, fetchedAt = 0;

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const since = () => (Date.now() - fetchedAt) / 1000;
  const n = v => (typeof v === 'number' && isFinite(v) ? v : null);
  const LEVEL = { ok: 'ok', warn: 'warn', running: 'run', disabled: 'unknown', unknown: 'unknown', failed: 'crit', overdue: 'crit', aborted: 'crit' };

  function dur(s) {
    s = n(s); if (s == null) return '—';
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    if (s < 172800) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`;
    return `${Math.round(s / 86400)} d`;
  }
  const ago = s => (n(s) == null ? '—' : `${dur(s)} ago`);
  const inT = s => (n(s) == null ? '—' : s <= 0 ? 'due now' : `in ${dur(s)}`);
  function every(s) {
    s = n(s); if (!s) return '';
    const d = s / 86400;
    if (Math.abs(d - 1) < 0.05) return 'daily';
    if (Math.abs(d - 7) < 0.3) return 'weekly';
    if (d >= 27 && d <= 32) return 'monthly';
    return `every ${dur(s)}`;
  }
  function bytes(b) {
    b = n(b); if (b == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b >= 100 || i === 0 ? Math.round(b) : b.toFixed(1)} ${u[i]}`;
  }
  const num = v => (n(v) == null ? '—' : v.toLocaleString('en-US'));

  // One bar per run (oldest left): height = data copied (log scale), colour = result.
  // Kopia (cloud) runs report changed files rather than bytes copied, so their bars use that.
  function history(runs, kind) {
    if (!runs?.length) return '<div class="thist empty">no runs recorded yet</div>';
    const kopia = kind === 'kopia';
    const metric = r => n(kopia ? r.summary?.files?.copied : r.summary?.bytes?.copied) ?? 0;
    const max = Math.max(1, ...runs.map(metric));
    const bars = runs.map(r => {
      const b = metric(r);
      const h = b > 0 ? 12 + 88 * Math.sqrt(b / max) : 8;
      const tip = `${ago(r.age_s + since())} · ${r.status}: ${r.message} · ${kopia ? num(b) + ' files changed' : bytes(b) + ' copied'} · took ${dur(r.duration_s)}`;
      return `<i class="lv-${LEVEL[r.status] || 'crit'}" style="height:${h.toFixed(0)}%" title="${esc(tip)}"></i>`;
    }).join('');
    return `<div class="thist">${bars}</div><div class="thist-axis"><span>${ago(runs[0].age_s + since())}</span><span>${runs.length} runs · height = ${kopia ? 'files changed' : 'data copied'}</span><span>latest</span></div>`;
  }

  function stats(r, kind) {
    const f = r?.summary?.files, b = r?.summary?.bytes;
    if (!f) return '';
    const cell = (label, value, sub, cls = '') => `<div class="tstat ${cls}"><span class="k">${label}</span><span class="v">${value}</span><span class="s">${sub}</span></div>`;
    if (kind === 'kopia') return `<div class="tstats">
      ${cell('Changed', num(f.copied) + ' files', 'uploaded, encrypted')}
      ${cell('Unchanged', num(f.skipped) + ' files', 'already in the cloud')}
      ${cell('Unreadable', num(f.failed), f.failed ? 'skipped this run' : 'none', f.failed ? 'bad' : '')}
      ${cell('Snapshot size', num(f.total) + ' files', bytes(b?.total))}
    </div>`;
    return `<div class="tstats">
      ${cell('Copied', num(f.copied) + ' files', bytes(b?.copied))}
      ${cell('Removed (extras)', num(f.extras) + ' files', bytes(b?.extras))}
      ${cell('Failed', num(f.failed), f.failed ? bytes(b?.failed) : 'none', f.failed ? 'bad' : '')}
      ${cell('Mirror size', num(f.total) + ' files', bytes(b?.total))}
    </div>`;
  }

  function card(host, t, hostStale) {
    const last = t.runs?.[t.runs.length - 1];
    const lv = LEVEL[t.health] || 'unknown';
    const running = n(t.running_for_s) != null ? t.running_for_s + since() : null;
    const lastLine = running != null ? `<b class="lv-run">Running</b> for ${dur(running)}`
      : last ? `<b class="lv-${LEVEL[last.status]}">${esc(last.status === 'ok' ? 'Succeeded' : last.status === 'warn' ? 'Completed with warnings' : last.status === 'aborted' ? 'Aborted' : 'Failed')}</b>
           ${ago(last.age_s + since())} · took ${dur(last.duration_s)} · <span class="dim">${esc(last.message)}</span>`
      : `<span class="dim">${t.last_run_age_s != null ? `last run ${ago(t.last_run_age_s + since())} (result 0x${((t.last_task_result ?? 0) >>> 0).toString(16).toUpperCase()})` : 'not run yet'}</span>`;
    const errors = last && last.errors?.length && !running
      ? `<div class="terrs">${last.errors.map(e => `<div>${esc(e)}</div>`).join('')}${last.error_count > last.errors.length ? `<div class="dim">… ${last.error_count - last.errors.length} more in the log</div>` : ''}</div>` : '';
    return `<article class="tcard lv-${lv}${hostStale ? ' stale' : ''}">
      <header>
        <span class="sd big"></span>
        <h2>${esc(t.job || t.name)}</h2>
        <span class="tag">${esc(host)}</span>
        ${t.schedule ? `<span class="tag">${esc(t.schedule)}</span>` : t.interval_s ? `<span class="tag">${esc(every(t.interval_s))}</span>` : ''}
        <span class="thealth lv-${lv}">${esc(t.health === 'ok' ? '' : t.health_text)}</span>
        <span class="tnext">${t.state === 'Disabled' ? 'disabled' : `next run ${inT(n(t.next_run_in_s) != null ? t.next_run_in_s - since() : null)}`}</span>
      </header>
      ${t.source ? `<div class="tpath"><code>${esc(t.source)}</code><span class="arrow">→</span><code>${esc(t.destination)}</code></div>` : t.description ? `<div class="tpath dim">${esc(t.description)}</div>` : ''}
      <div class="tlast">${lastLine}</div>
      ${running == null ? stats(last, t.kind) : ''}
      ${errors}
      ${t.job ? history(t.runs, t.kind) : ''}
    </article>`;
  }

  function render() {
    if (!data) return;
    const all = (data.hosts || []).flatMap(h => h.tasks.map(t => ({ host: h.host, stale: h.stale, t })));
    $('tasksEmpty').hidden = all.length > 0;
    $('tasksList').innerHTML = all.map(x => card(x.host, x.t, x.stale)).join('');
    const bad = all.filter(x => ['failed', 'overdue'].includes(x.t.health)).length;
    const running = all.filter(x => x.t.health === 'running').length;
    $('tasksSummary').innerHTML = `<span class="chip">${all.length} tasks</span>` +
      (bad ? `<span class="chip lv-crit">${bad} need attention</span>` : all.length ? '<span class="chip lv-ok">all good</span>' : '') +
      (running ? `<span class="chip">${running} running</span>` : '');
  }

  async function refresh() {
    try {
      const r = await fetch('api/tasks', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
      fetchedAt = Date.now();
    } catch (e) {
      console.warn('tasks: fetch failed', e);
    }
    render();
  }

  document.addEventListener('screenchange', e => { if (e.detail.id === 'tasks') refresh(); });
  setInterval(refresh, REFRESH_MS);
  setInterval(render, 5000);
  refresh();
})();
