/* Rack Dashboard - Deployments screen. Reads GET /api/deploy; "Deploy all" POSTs it. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 5000, FAST_MS = 2000, FAST_FOR_S = 150;
  let data = null, fetchedAt = 0, timer = null;

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const short = c => (c || '').slice(0, 7);
  const OS_TEXT = { windows: 'Windows', linux: 'Linux', freebsd: 'FreeBSD' };
  const since = () => (Date.now() - fetchedAt) / 1000;          // seconds since the last fetch
  const age = s => (s == null ? null : s + since());
  const n = v => (typeof v === 'number' && isFinite(v) ? v : null);
  function dur(s) {
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 5400) return `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, '0')}s`;
    return `${(s / 3600).toFixed(1)} h`;
  }

  function ago(s) {
    if (s == null) return '—';
    if (s < 60) return `${Math.round(s)} s ago`;
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 172800) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function every(s) {
    if (!s) return '';
    return s % 3600 === 0 ? `every ${s / 3600} h` : s % 60 === 0 ? `every ${s / 60} min` : `every ${s} s`;
  }
  function inTime(s) {
    if (s == null) return '';
    if (s <= 5) return 'due now';
    return s < 90 ? `next in ${Math.round(s)} s` : `next in ${Math.round(s / 60)} min`;
  }

  // Where is this host with the current deploy request?
  // Updater working right now (with or without a deploy request): how long, and which step.
  function running(h) {
    if (h.stale || n(h.running_for_s) == null) return null;
    const s = age(h.running_for_s);
    return s > 3600 ? { lv: 'warn', text: `running ${dur(s)}, stuck?` } : { lv: 'unknown', text: `running ${dur(s)}` };
  }

  function progress(h, req) {
    const r = running(h);
    if (r) return r;
    if (!req || age(req.age_s) > 300) return null;
    const checked = age(h.checked_age_s);
    if (h.stale) return { lv: 'warn', text: 'offline' };
    if (checked != null && checked < age(req.age_s)) {
      if (h.result !== 'ok') return { lv: 'crit', text: h.result };
      return data.reference_commit && h.commit !== data.reference_commit
        ? { lv: 'warn', text: 'checked, behind' } : { lv: 'ok', text: 'done' };
    }
    return { lv: 'unknown', text: 'updating…' };
  }

  function row(h, req) {
    const behind = data.reference_commit && h.commit && h.commit !== data.reference_commit;
    const lv = !h.reported ? 'unknown' : h.stale ? 'warn' : h.result === 'ok' ? (behind ? 'warn' : 'ok') : 'crit';
    const checked = age(h.checked_age_s);
    const next = h.every_s && checked != null ? h.every_s - checked : null;
    const p = progress(h, req);
    const resultText = !h.reported ? 'no status yet (agent not updated)'
      : h.stale ? `host offline · last seen ${ago(age(h.seen_age_s))}`
      : behind && h.result === 'ok' ? 'behind: main changed after its last check' : (h.message || h.result);
    return `<tr class="lv-${lv}">
      <td class="hostc"><span class="sd"></span><b>${esc(h.host)}</b><span class="os">${esc(OS_TEXT[h.os] || h.os || '')}</span></td>
      <td><span class="tag branch">${esc(h.branch || '—')}</span></td>
      <td class="commitc">
        <span class="hash ${behind ? 'behind' : ''}" title="${esc(h.commit || '')}">${esc(short(h.commit) || '—')}</span>
        <span class="subj">${esc(h.subject || '')}</span>
        <span class="dim">${h.committed_age_s != null ? `committed ${ago(age(h.committed_age_s))}` : ''}${behind ? ` · dashboard is on ${esc(short(data.reference_commit))}` : ''}</span>
      </td>
      <td class="num">${ago(checked)}<span class="dim">${running(h) ? esc(h.phase || 'updating') : `${esc(every(h.every_s))}${next != null && !h.stale ? ` · ${inTime(next)}` : ''}`}</span></td>
      <td class="num">${ago(age(h.changed_age_s))}</td>
      <td class="resc"><span class="res lv-${lv}">${esc(resultText)}</span></td>
      <td class="num">${esc(h.git || '—')}<span class="dim">${esc(h.agent || '')}${h.agent_status?.outdated ? ' · outdated' : ''}</span></td>
      <td class="progc">${p ? `<span class="chip lv-${p.lv}">${esc(p.text)}</span>` : ''}</td>
    </tr>`;
  }

  function render() {
    if (!data) return;
    const hosts = data.hosts || [], req = data.request;
    $('deployEmpty').hidden = hosts.length > 0;
    $('deployRows').innerHTML = hosts.map(h => row(h, req)).join('');

    const reqAge = req ? age(req.age_s) : null;
    const btn = $('deployAllBtn');
    btn.disabled = reqAge != null && reqAge < 60;
    btn.title = btn.disabled ? `Already requested ${Math.round(reqAge)} s ago (once a minute at most)` : 'Ask every host to pull and deploy main now';

    const ref = data.reference_commit;
    const onRef = ref ? hosts.filter(h => h.commit === ref).length : 0;
    const failing = hosts.filter(h => h.reported && h.result !== 'ok').length;
    let chips = ref ? `<span class="chip ${onRef === hosts.length ? 'lv-ok' : 'lv-warn'}">${onRef}/${hosts.length} on ${esc(short(ref))}</span>` : '';
    if (failing) chips += `<span class="chip lv-crit">${failing} failing</span>`;
    if (reqAge != null && reqAge <= 300) {
      const done = hosts.filter(h => progress(h, req)?.text === 'done').length;
      chips += `<span class="chip">deploy requested ${ago(reqAge)} · ${done}/${hosts.length} done</span>`;
    }
    $('deploySummary').innerHTML = chips;
  }

  async function refresh() {
    try {
      const r = await fetch('api/deploy', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
      fetchedAt = Date.now();
    } catch (e) {
      console.warn('deploy: fetch failed', e);
    }
    render();
    schedule();
  }

  function schedule() {   // poll faster while a deploy is rolling out
    clearTimeout(timer);
    const reqAge = data?.request ? age(data.request.age_s) : null;
    timer = setTimeout(refresh, reqAge != null && reqAge < FAST_FOR_S ? FAST_MS : REFRESH_MS);
  }

  async function deployAll() {
    const btn = $('deployAllBtn'), note = $('deployNote');
    btn.disabled = true;
    note.textContent = '';
    try {
      const r = await fetch('api/deploy', { method: 'POST', headers: { 'X-Rack-Deploy': '1' } });
      const j = await r.json().catch(() => ({}));
      if (!r.ok && r.status !== 429) throw new Error(j.error || `HTTP ${r.status}`);
    } catch (e) {
      note.textContent = `Couldn't request a deploy: ${e.message}`;
    }
    refresh();
  }

  $('deployAllBtn').addEventListener('click', deployAll);
  document.addEventListener('screenchange', e => { if (e.detail.id === 'deploy') refresh(); });
  setInterval(render, 1000);   // keep the "ago" times ticking between fetches
  refresh();
})();
