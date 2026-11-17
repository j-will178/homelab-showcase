/* Rack Dashboard - static demo (GitHub Pages). Stands in for the collector's api/*
 * endpoints with a snapshot of its own demo data (demo-data.json), and keeps it moving:
 *  - compute: replays the 1-hour history, one step every 10 s (charts scroll, numbers change);
 *  - Deploy all: plays a rollout (each host "running" -> "done" on the new commit).
 * Loaded before the dashboard's own scripts; everything else is the real UI. */
(() => {
  'use strict';
  const realFetch = window.fetch.bind(window);
  const t0 = Date.now();
  // ?speed=4 plays the demo 4x faster (used to record the README animation).
  const SPEED = Math.max(1, Number(new URLSearchParams(location.search).get('speed')) || 1);
  const elapsed = () => SPEED * (Date.now() - t0) / 1000;
  const clone = o => JSON.parse(JSON.stringify(o));
  let data = null;
  const ready = realFetch('demo-data.json').then(r => r.json()).then(d => { data = d; });

  // ---- compute: rotate the history and take the "current" values from its newest point ----
  function metricsNow() {
    const m = clone(data.metrics);
    const step = Math.floor(elapsed() / 10);
    for (const h of m.hosts) {
      const hist = h.history || [];
      if (!hist.length) continue;
      const k = step % hist.length;
      h.history = hist.slice(k).concat(hist.slice(0, k));
      const p = h.history[h.history.length - 1];
      h.age_s = elapsed() % 10;
      if (h.cpu && p.cpu != null) {
        const base = h.cpu.util_pct || 1;
        h.cpu.util_pct = p.cpu;
        if (h.cpu.per_core_pct) h.cpu.per_core_pct = h.cpu.per_core_pct.map(v => Math.max(0, Math.min(100, v * p.cpu / base)));
      }
      if (h.mem && p.mem != null && h.mem.total) h.mem.used = h.mem.total * p.mem / 100;
      if (h.gpus && h.gpus[0] && p.gpu != null) h.gpus[0].util_pct = p.gpu;
      if (h.net && h.net[0]) { if (p.rx != null) h.net[0].rx_bps = p.rx; if (p.tx != null) h.net[0].tx_bps = p.tx; }
      if (h.disks && h.disks[0]) { if (p.rd != null) h.disks[0].read_bps = p.rd; if (p.wr != null) h.disks[0].write_bps = p.wr; }
    }
    return m;
  }

  // ---- Deploy all: a scripted rollout of a new commit ----
  const NEW = { commit: 'd3m0c0de5e1f', subject: 'Demo: a merged pull request' };
  const PLAN = {   // seconds after the press: picked up, then running for
    'optiplex-management': [4, 14, 'running tests and restarting what changed'],
    'opnsense': [9, 8, 'pulling from GitHub'],
    'backup-compute': [12, 22, 'applying scheduled tasks'],
    'homeshare': [7, 48, 'applying scheduled tasks'],
  };
  let deployAt = null;
  function deployNow() {
    const d = clone(data.deploy);
    if (deployAt === null) return d;
    const age = SPEED * (Date.now() - deployAt) / 1000;
    d.request = { id: Math.floor(deployAt / 1000), age_s: age, retry_in_s: Math.max(0, Math.round(60 - age)) };
    d.reference_commit = NEW.commit;
    for (const h of d.hosts) {
      const [start, run, phase] = PLAN[h.host] || [5, 10, 'updating'];
      if (age < start) continue;
      if (age < start + run) { h.running_for_s = age - start; h.phase = phase; continue; }
      const since = age - start - run;
      Object.assign(h, NEW, { result: 'ok', message: `updated to ${NEW.commit.slice(0, 7)}`, checked_age_s: since,
                              changed_age_s: since, committed_age_s: age + 30, running_for_s: null, stale: false });
    }
    return d;
  }

  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const m = /^(?:\.\/)?api\/(\w+)/.exec(url);
    if (!m) return realFetch(input, init);
    await ready;
    const method = (init && init.method) || 'GET';
    switch (m[1]) {
      case 'smart': return json(data.smart);
      case 'metrics': return json(metricsNow());
      case 'tasks': return json(data.tasks);
      case 'deploy':
        if (method === 'POST') {
          if (deployAt !== null && (Date.now() - deployAt) / 1000 < 60) {
            return json({ error: 'deploy already requested', deploy: deployNow().request }, 429);
          }
          deployAt = Date.now();
          return json({ ok: true, deploy: deployNow().request }, 202);
        }
        return json(deployNow());
      default: return json({ error: 'not in the demo' }, 404);
    }
  };

  // ---- a small badge so visitors know what they're looking at ----
  window.addEventListener('DOMContentLoaded', async () => {
    let repo = '';
    try { repo = (await (await realFetch('config.json')).json()).repo_url || ''; } catch { /* no link */ }
    const b = document.createElement('div');
    b.className = 'demo-badge';
    b.innerHTML = `Live demo · simulated data · <kbd>←</kbd><kbd>→</kbd> switch screens${repo ? ` · <a href="${repo}" target="_blank" rel="noopener">source on GitHub</a>` : ''}`;
    const s = document.createElement('style');
    s.textContent = `.demo-badge{position:fixed;left:14px;bottom:14px;z-index:9999;padding:6px 14px;border-radius:999px;
      background:rgba(10,14,20,.86);border:1px solid rgba(255,255,255,.14);color:#cbd5e1;font:12px/1.4 system-ui,sans-serif;backdrop-filter:blur(6px)}
      .demo-badge a{color:#7dd3fc}.demo-badge kbd{font:11px ui-monospace,monospace;border:1px solid rgba(255,255,255,.2);border-radius:4px;padding:0 4px;margin:0 1px}`;
    document.head.appendChild(s);
    document.body.appendChild(b);
  });
})();
