/* Rack Dashboard - WAN noise map screen. Plain JS + d3 (no build step). */
(() => {
  'use strict';

  // ---------- settings ----------
  const qs = new URLSearchParams(location.search);
  const store = {
    get(k, d) { try { const v = localStorage.getItem('noisemap.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem('noisemap.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  const PROJECTIONS = [
    { id: 'mercator',     name: 'Mercator',                     make: () => d3.geoMercator(), fitLand: true },
    { id: 'miller',       name: 'Miller cylindrical',           make: () => d3.geoMiller() },
    { id: 'equirect',     name: 'Equirectangular (Plate Carrée)', make: () => d3.geoEquirectangular() },
    { id: 'robinson',     name: 'Robinson',                     make: () => d3.geoRobinson() },
    { id: 'naturalEarth', name: 'Natural Earth I',              make: () => d3.geoNaturalEarth1() },
    { id: 'equalEarth',   name: 'Equal Earth',                  make: () => d3.geoEqualEarth() },
    { id: 'winkel3',      name: 'Winkel tripel',                make: () => d3.geoWinkel3() },
    { id: 'kavrayskiy7',  name: 'Kavrayskiy VII',               make: () => d3.geoKavrayskiy7() },
    { id: 'eckert4',      name: 'Eckert IV',                    make: () => d3.geoEckert4() },
    { id: 'wagner6',      name: 'Wagner VI',                    make: () => d3.geoWagner6() },
    { id: 'mollweide',    name: 'Mollweide',                    make: () => d3.geoMollweide() },
    { id: 'sinusoidal',   name: 'Sinusoidal',                   make: () => d3.geoSinusoidal() },
  ];

  const CATEGORIES = [
    { id: 'remote', label: 'Remote access', color: '#ffb547', ports: [22, 23, 2222, 2323, 3389, 5555, 5900, 5901] },
    { id: 'web',    label: 'Web',           color: '#4fd1ff', ports: [80, 443, 8000, 8008, 8080, 8081, 8443, 8888, 9000] },
    { id: 'db',     label: 'Windows / DB',  color: '#ff5fa2', ports: [135, 137, 139, 445, 1433, 1521, 3306, 5432, 6379, 9200, 11211, 27017] },
    { id: 'iot',    label: 'IoT / VoIP',    color: '#a3e635', ports: [161, 1900, 5060, 5061, 7547, 8291, 37215, 52869] },
    { id: 'udp',    label: 'Other UDP',     color: '#a78bfa' },
    { id: 'tcp',    label: 'Other TCP',     color: '#5eead4' },
    { id: 'other',  label: 'ICMP / other',  color: '#94a3b8' },
  ];
  const CAT = Object.fromEntries(CATEGORIES.map(c => [c.id, c]));
  const PORT_CAT = new Map();
  CATEGORIES.forEach(c => (c.ports || []).forEach(p => PORT_CAT.set(p, c)));
  const categorize = ev =>
    (ev.dport != null && PORT_CAT.get(ev.dport)) ||
    (ev.proto === 'udp' ? CAT.udp : ev.proto === 'tcp' ? CAT.tcp : CAT.other);

  const SERVICES = {
    21: 'ftp', 22: 'ssh', 23: 'telnet', 25: 'smtp', 53: 'dns', 80: 'http', 110: 'pop3', 123: 'ntp', 135: 'msrpc',
    139: 'netbios', 143: 'imap', 161: 'snmp', 443: 'https', 445: 'smb', 1433: 'mssql', 1521: 'oracle', 1900: 'ssdp',
    2222: 'ssh-alt', 2323: 'telnet-alt', 3306: 'mysql', 3389: 'rdp', 5060: 'sip', 5432: 'postgres', 5555: 'adb',
    5900: 'vnc', 6379: 'redis', 7547: 'tr-069', 8080: 'http-alt', 8291: 'mikrotik', 8443: 'https-alt',
    9200: 'elastic', 11211: 'memcached', 27017: 'mongodb', 37215: 'huawei', 52869: 'upnp',
  };

  const state = {
    projIdx: Math.max(0, PROJECTIONS.findIndex(p => p.id === (qs.get('projection') || store.get('projection', 'robinson')))),
    fade: Number(qs.get('fade')) || store.get('fade', 60),
    arcs: store.get('arcs', false),
    dayNight: qs.has('daynight') ? qs.get('daynight') !== '0' : store.get('dayNight', true),
    home: null, pickingHome: false,
    pulses: [], arcList: [],
    total: 0, times: [], feedQueue: [],
    countries: new Map(), ports: new Map(),
    dirty: true,
  };

  // ---------- DOM ----------
  const $ = id => document.getElementById(id);
  const stage = $('stage'), svg = d3.select('#map'), canvas = $('fx'), ctx = canvas.getContext('2d');
  const tooltip = $('tooltip');
  let W = 0, H = 0, projection = null, land = null, borders = null, fitLand = null;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const flag = cc => (cc && cc.length === 2) ? String.fromCodePoint(...[...cc.toUpperCase()].map(c => 127397 + c.charCodeAt(0))) : '🏳';
  const hex2rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));

  // Pre-rendered glow sprites, one per category colour.
  const SPRITES = {};
  for (const c of CATEGORIES) {
    const s = document.createElement('canvas'); s.width = s.height = 64;
    const g = s.getContext('2d'), [r, gr, b] = hex2rgb(c.color);
    const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(0.12, `rgba(${r},${gr},${b},1)`);
    grad.addColorStop(0.35, `rgba(${r},${gr},${b},0.35)`);
    grad.addColorStop(1, `rgba(${r},${gr},${b},0)`);
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    SPRITES[c.id] = { img: s, rgb: [r, gr, b] };
  }

  // ---------- map ----------
  async function loadWorld() {
    const topo = await (await fetch('vendor/countries-50m.json')).json();
    land = topojson.feature(topo, topo.objects.land);
    borders = topojson.mesh(topo, topo.objects.countries, (a, b) => a !== b);
    const countries = topojson.feature(topo, topo.objects.countries);
    fitLand = { type: 'FeatureCollection', features: countries.features.filter(f => f.id !== '010') }; // minus Antarctica
  }

  // ---------- day / night ----------
  // Subsolar point (where the sun is directly overhead), from the standard
  // low-precision solar formulas (accurate to ~0.01 deg, plenty for a map).
  function subsolarPoint(date = new Date()) {
    const rad = Math.PI / 180, d = date / 86400000 + 2440587.5 - 2451545.0;   // days since J2000
    const g = (357.529 + 0.98560028 * d) * rad;                              // mean anomaly
    const q = 280.459 + 0.98564736 * d;                                      // mean longitude
    const L = (q + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * rad;      // ecliptic longitude
    const e = (23.439 - 0.00000036 * d) * rad;                               // obliquity
    const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) / rad;     // right ascension
    const dec = Math.asin(Math.sin(e) * Math.sin(L)) / rad;                  // declination = latitude
    const gmst = (18.697374558 + 24.06570982441908 * d) % 24;                // sidereal time, hours
    const lon = ((ra - gmst * 15) % 360 + 540) % 360 - 180;
    return [lon, dec];
  }
  window.subsolarPoint = subsolarPoint;   // for tests / the console

  let nightLayer = null;
  function drawNight() {
    if (!nightLayer || !projection) return;
    nightLayer.selectAll('*').remove();
    if (!state.dayNight) return;
    const [lon, lat] = subsolarPoint();
    const anti = [lon + 180, -lat];
    const path = d3.geoPath(projection);
    // Night = sun below the horizon = within 90 deg of the antisolar point.
    // Civil / nautical / astronomical twilight end 6 / 12 / 18 deg further in;
    // stacked translucent steps give a soft edge that deepens into full night.
    // Day side: a faint glow, strongest under the sun.
    for (let r = 90; r >= 10; r -= 10) {
      nightLayer.append('path').attr('class', 'day')
        .attr('d', path(d3.geoCircle().center([lon, lat]).radius(r).precision(1)()));
    }
    // Night: 3-degree steps from the horizon (90) to astronomical night (72).
    for (let r = 90; r >= 72; r -= 3) {
      nightLayer.append('path').attr('class', 'night')
        .attr('d', path(d3.geoCircle().center(anti).radius(r).precision(1)()));
    }
    nightLayer.append('path').attr('class', 'terminator')
      .attr('d', path(d3.geoCircle().center(anti).radius(90).precision(1)()));
    const sun = projection([lon, lat]);
    if (sun && sun.every(Number.isFinite)) {
      nightLayer.append('circle').attr('class', 'sun-glow').attr('cx', sun[0]).attr('cy', sun[1]).attr('r', 14);
      nightLayer.append('circle').attr('class', 'sun').attr('cx', sun[0]).attr('cy', sun[1]).attr('r', 3.2);
    }
  }
  setInterval(drawNight, 60000);

  function render() {
    const rect = stage.getBoundingClientRect();
    W = rect.width; H = rect.height;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const p = PROJECTIONS[state.projIdx];
    projection = p.make();
    const pad = 22, extent = [[pad, pad + 34], [W - pad, H - pad - 14]];
    projection.fitExtent(extent, p.fitLand ? fitLand : { type: 'Sphere' });
    const path = d3.geoPath(projection);

    svg.attr('viewBox', `0 0 ${W} ${H}`).selectAll('*').remove();
    svg.append('path').attr('class', 'sphere').attr('d', path({ type: 'Sphere' }));
    svg.append('path').attr('class', 'grat').attr('d', path(d3.geoGraticule10()));
    svg.append('path').attr('class', 'land').attr('d', path(land));
    svg.append('path').attr('class', 'borders').attr('d', path(borders));
    nightLayer = svg.append('g').attr('class', 'daynight');
    drawNight();
    if (state.home) {
      const xy = projection([state.home[1], state.home[0]]);
      if (xy) svg.append('circle').attr('class', 'home').attr('cx', xy[0]).attr('cy', xy[1]).attr('r', 5);
    }
  }

  // ---------- events ----------
  function addEvent(ev) {
    if (typeof ev.lat !== 'number' || typeof ev.lon !== 'number') return;
    const cat = categorize(ev), now = performance.now();
    state.pulses.push({ lon: ev.lon, lat: ev.lat, t0: now, cat: cat.id, ev });
    if (state.pulses.length > 5000) state.pulses.splice(0, state.pulses.length - 5000);

    if (state.arcs && state.home) {
      state.arcList.push({ t0: now, cat: cat.id, interp: d3.geoInterpolate([ev.lon, ev.lat], [state.home[1], state.home[0]]) });
      if (state.arcList.length > 150) state.arcList.shift();
    }

    state.total++;
    state.times.push(Date.now());
    state.feedQueue.push({ ...ev, color: cat.color });
    if (state.feedQueue.length > 25) state.feedQueue.shift();   // stay near-live during bursts
    const ck = ev.cc || '??';
    const c = state.countries.get(ck) || { n: 0, name: ev.country || 'Unknown', cc: ev.cc };
    c.n++; state.countries.set(ck, c);
    const pk = ev.dport != null ? `${ev.dport}/${ev.proto}` : ev.proto;
    const p = state.ports.get(pk) || { n: 0, port: ev.dport, proto: ev.proto, color: cat.color };
    p.n++; state.ports.set(pk, p);
    state.dirty = true;
  }

  // ---------- animation ----------
  const RING_MS = 1800, FLASH_MS = 600, ARC_DRAW_MS = 1100, ARC_FADE_MS = 2400, TAU = Math.PI * 2;
  const easeOut = k => 1 - Math.pow(1 - k, 3);

  function frame(now) {
    try { draw(now); } catch (err) { console.error(err); }
    requestAnimationFrame(frame);
  }

  function draw(now) {
    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';
    const fadeMs = state.fade * 1000;
    const ringMax = Math.max(10, Math.min(26, W / 55));

    let keep = 0;
    for (const e of state.pulses) {
      const age = Math.max(0, now - e.t0);   // rAF time can trail performance.now()
      if (age > fadeMs) continue;
      state.pulses[keep++] = e;
      const xy = projection && projection([e.lon, e.lat]);
      if (!xy) continue;
      const [x, y] = xy, sp = SPRITES[e.cat];

      if (age < RING_MS) {                                   // expanding ring
        const k = age / RING_MS;
        ctx.globalAlpha = (1 - k) * 0.8;
        ctx.strokeStyle = `rgb(${sp.rgb})`;
        ctx.lineWidth = 0.6 + 1.4 * (1 - k);
        ctx.beginPath(); ctx.arc(x, y, 3 + easeOut(k) * ringMax, 0, TAU); ctx.stroke();
      }
      const flash = Math.max(0, 1 - age / FLASH_MS);          // bright initial ping
      const life = Math.pow(1 - age / fadeMs, 1.6);          // slow afterglow
      const size = 11 + 26 * flash;
      ctx.globalAlpha = Math.min(1, 0.55 * life + 0.45 * flash);
      ctx.drawImage(sp.img, x - size / 2, y - size / 2, size, size);
    }
    state.pulses.length = keep;

    if (state.home && state.arcs) {
      let k2 = 0;
      for (const a of state.arcList) {
        const age = Math.max(0, now - a.t0);
        if (age > ARC_DRAW_MS + ARC_FADE_MS) continue;
        state.arcList[k2++] = a;
        const head = Math.min(1, age / ARC_DRAW_MS);
        const alpha = age < ARC_DRAW_MS ? 0.55 : 0.55 * (1 - (age - ARC_DRAW_MS) / ARC_FADE_MS);
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = `rgb(${SPRITES[a.cat].rgb})`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        let prev = null;
        for (let i = 0; i <= 40; i++) {
          const pt = projection(a.interp((i / 40) * head));
          if (!pt) { prev = null; continue; }
          if (!prev || Math.abs(pt[0] - prev[0]) > W / 2) ctx.moveTo(pt[0], pt[1]); else ctx.lineTo(pt[0], pt[1]);
          prev = pt;
        }
        ctx.stroke();
      }
      state.arcList.length = k2;
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
  }

  // ---------- panel ----------
  function bars(el, items, labelFn) {
    const max = items.length ? items[0].n : 1;
    el.innerHTML = items.map(it =>
      `<li><div class="fill" style="width:${(it.n / max * 100).toFixed(1)}%"></div>` +
      `<span>${labelFn(it)}</span><span class="n">${it.n.toLocaleString()}</span></li>`).join('');
  }

  function updatePanel() {
    const cutoff = Date.now() - 60000;
    while (state.times.length && state.times[0] < cutoff) state.times.shift();
    $('rate').textContent = state.times.length.toLocaleString();
    $('total').textContent = state.total.toLocaleString();
    if (!state.dirty) return;
    state.dirty = false;

    const topC = [...state.countries.values()].sort((a, b) => b.n - a.n).slice(0, 6);
    bars($('topCountries'), topC, c => `${flag(c.cc)} ${esc(c.name)}`);
    const topP = [...state.ports.values()].sort((a, b) => b.n - a.n).slice(0, 6);
    bars($('topPorts'), topP, p =>
      `<span class="dot" style="color:${p.color};display:inline-block;margin-right:6px"></span>` +
      (p.port != null ? `${p.port}/${p.proto} ${esc(SERVICES[p.port] || '')}` : esc(p.proto)));

  }

  // Live feed: add one row at a time so it slides in, instead of rebuilding the list.
  const FEED_MAX = 30, FEED_STEP_MS = 280;
  function feedTick() {
    const e = state.feedQueue.shift();
    if (!e) return;
    const feed = $('feed');
    const ts = new Date(e.t || Date.now()).toLocaleTimeString([], { hour12: false });
    const where = [e.city, e.country].filter(Boolean).join(', ');
    const port = e.dport != null ? `${e.dport}` : e.proto;
    const li = document.createElement('li');
    li.className = 'enter';
    li.innerHTML = `<span class="ts">${ts}</span><span class="where" title="${esc(e.ip)}">${flag(e.cc)} ${esc(where || e.ip)}</span>` +
                   `<span class="port" style="color:${e.color}">${esc(port)}</span>`;
    feed.prepend(li);
    requestAnimationFrame(() => requestAnimationFrame(() => li.classList.remove('enter')));
    while (feed.children.length > FEED_MAX) feed.lastElementChild.remove();
  }

  function buildControls() {
    const sel = $('projection');
    sel.innerHTML = PROJECTIONS.map((p, i) => `<option value="${i}">${p.name}</option>`).join('');
    sel.value = state.projIdx;
    sel.onchange = () => setProjection(Number(sel.value));

    const fade = $('fade');
    const fadeText = () => { $('fadeLabel').textContent = state.fade >= 60 ? `${(state.fade / 60).toFixed(state.fade % 60 ? 1 : 0)} min` : `${state.fade} s`; };
    fade.value = state.fade; fadeText();
    fade.oninput = () => { state.fade = Number(fade.value); store.set('fade', state.fade); fadeText(); };

    const arcs = $('arcs');
    const dn = $('dayNight');
    dn.checked = state.dayNight;
    dn.onchange = () => { state.dayNight = dn.checked; store.set('dayNight', state.dayNight); drawNight(); };
    arcs.checked = state.arcs;
    arcs.onchange = () => {
      state.arcs = arcs.checked; store.set('arcs', state.arcs);
      if (state.arcs && !state.home) startPickHome();
    };
    updateHomeRow();

    $('legend').innerHTML = CATEGORIES.map(c => `<li><span class="dot" style="color:${c.color}"></span>${c.label}</li>`).join('');
    if (store.get('panelHidden', false)) $('panel').classList.add('hidden');
  }

  function updateHomeRow() {
    const row = $('homeRow');
    if (state.home) {
      row.innerHTML = `Home: ${state.home[0].toFixed(1)}°, ${state.home[1].toFixed(1)}°` +
        `<button id="homeChange">change</button><button id="homeClear">clear</button>`;
      $('homeChange').onclick = startPickHome;
      $('homeClear').onclick = () => setHome(null);
    } else {
      row.innerHTML = `No home set<button id="homeSet">set on map</button>`;
      $('homeSet').onclick = startPickHome;
    }
  }

  function setHome(h) {
    state.home = h;
    store.set('home', h);
    state.arcList = [];
    if (!h) { state.arcs = false; $('arcs').checked = false; store.set('arcs', false); }
    updateHomeRow();
    render();
  }

  function startPickHome() {
    state.pickingHome = true;
    stage.classList.add('picking');
    if (!$('pickBanner')) {
      const b = document.createElement('div');
      b.id = 'pickBanner'; b.textContent = 'Click the map to set home · Esc to cancel';
      stage.appendChild(b);
    }
  }

  function stopPickHome() {
    state.pickingHome = false;
    stage.classList.remove('picking');
    $('pickBanner')?.remove();
    if (!state.home && state.arcs) { state.arcs = false; $('arcs').checked = false; store.set('arcs', false); }
  }

  stage.addEventListener('click', e => {
    if (!state.pickingHome || !projection) return;
    const r = stage.getBoundingClientRect(), pt = [e.clientX - r.left, e.clientY - r.top];
    const ll = projection.invert(pt);
    if (!ll || !ll.every(Number.isFinite)) return;
    const back = projection(ll);                       // reject clicks outside the map outline
    if (!back || Math.hypot(back[0] - pt[0], back[1] - pt[1]) > 1.5) return;
    const lat = Math.max(-85, Math.min(85, ll[1]));
    setHome([Math.round(lat * 100) / 100, Math.round(ll[0] * 100) / 100]);
    state.arcs = true; $('arcs').checked = true; store.set('arcs', true);
    stopPickHome();
  });

  function setProjection(i) {
    state.projIdx = (i + PROJECTIONS.length) % PROJECTIONS.length;
    $('projection').value = state.projIdx;
    store.set('projection', PROJECTIONS[state.projIdx].id);
    render();
  }

  function setStatus(kind, text) {
    const el = $('status');
    el.className = 'pill ' + kind;
    el.textContent = text;
  }

  // Hover: show details for the nearest recent pulse.
  stage.addEventListener('mousemove', e => {
    const r = stage.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null, bestD = 100;
    for (let i = state.pulses.length - 1, n = 0; i >= 0 && n < 600; i--, n++) {
      const p = state.pulses[i], xy = projection([p.lon, p.lat]);
      if (!xy) continue;
      const d = (xy[0] - mx) ** 2 + (xy[1] - my) ** 2;
      if (d < bestD) { bestD = d; best = p; }
    }
    if (!best) { tooltip.hidden = true; return; }
    const ev = best.ev;
    tooltip.innerHTML = `${flag(ev.cc)} ${esc(ev.ip)}<br>${esc([ev.city, ev.country].filter(Boolean).join(', '))}<br>` +
      `${esc(ev.proto)}${ev.dport != null ? ' → ' + ev.dport + ' ' + esc(SERVICES[ev.dport] || '') : ''}`;
    tooltip.style.left = Math.min(mx + 14, W - 180) + 'px';
    tooltip.style.top = (my + 14) + 'px';
    tooltip.hidden = false;
  });
  stage.addEventListener('mouseleave', () => { tooltip.hidden = true; });

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT') return;
    if (document.body.dataset.screen !== 'noise') return;   // only while this screen is showing
    const k = e.key.toLowerCase();
    if (k === 'escape' && state.pickingHome) { stopPickHome(); return; }
    if (k === 'p') setProjection(state.projIdx + (e.shiftKey ? -1 : 1));
    else if (k === 'h') {
      const hidden = $('panel').classList.toggle('hidden');
      store.set('panelHidden', hidden);
      render();
    }
  });

  document.addEventListener('screenchange', e => { if (e.detail.id === 'noise') render(); });

  let resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 120); });

  // ---------- data sources ----------
  function connectLive() {
    const es = new EventSource('events');
    es.onopen = () => setStatus('live', 'live');
    es.onmessage = m => { try { addEvent(JSON.parse(m.data)); } catch { /* ignore bad event */ } };
    es.onerror = () => setStatus('off', 'reconnecting');
  }

  const DEMO_SOURCES = [ // lat, lon, city, cc, country, weight
    [39.04, -77.49, 'Ashburn', 'US', 'United States', 14], [37.39, -121.96, 'Santa Clara', 'US', 'United States', 6],
    [40.71, -74.0, 'New York', 'US', 'United States', 4], [32.78, -96.8, 'Dallas', 'US', 'United States', 4],
    [52.37, 4.9, 'Amsterdam', 'NL', 'Netherlands', 9], [50.11, 8.68, 'Frankfurt am Main', 'DE', 'Germany', 7],
    [51.51, -0.13, 'London', 'GB', 'United Kingdom', 5], [48.86, 2.35, 'Paris', 'FR', 'France', 3],
    [55.75, 37.62, 'Moscow', 'RU', 'Russia', 6], [59.94, 30.31, 'Saint Petersburg', 'RU', 'Russia', 2],
    [39.9, 116.4, 'Beijing', 'CN', 'China', 7], [31.23, 121.47, 'Shanghai', 'CN', 'China', 6],
    [22.54, 114.06, 'Shenzhen', 'CN', 'China', 4], [37.57, 126.98, 'Seoul', 'KR', 'South Korea', 3],
    [35.68, 139.69, 'Tokyo', 'JP', 'Japan', 2], [1.35, 103.82, 'Singapore', 'SG', 'Singapore', 5],
    [19.08, 72.88, 'Mumbai', 'IN', 'India', 4], [28.61, 77.21, 'New Delhi', 'IN', 'India', 2],
    [-23.55, -46.63, 'São Paulo', 'BR', 'Brazil', 5], [21.03, 105.85, 'Hanoi', 'VN', 'Vietnam', 3],
    [10.82, 106.63, 'Ho Chi Minh City', 'VN', 'Vietnam', 2], [-6.21, 106.85, 'Jakarta', 'ID', 'Indonesia', 2],
    [41.01, 28.98, 'Istanbul', 'TR', 'Türkiye', 2], [50.45, 30.52, 'Kyiv', 'UA', 'Ukraine', 2],
    [44.43, 26.1, 'Bucharest', 'RO', 'Romania', 2], [43.65, -79.38, 'Toronto', 'CA', 'Canada', 2],
    [-33.87, 151.21, 'Sydney', 'AU', 'Australia', 1], [6.52, 3.38, 'Lagos', 'NG', 'Nigeria', 1],
    [25.2, 55.27, 'Dubai', 'AE', 'United Arab Emirates', 1], [-34.6, -58.38, 'Buenos Aires', 'AR', 'Argentina', 1],
    [52.23, 21.01, 'Warsaw', 'PL', 'Poland', 2], [60.17, 24.94, 'Helsinki', 'FI', 'Finland', 1],
  ];
  const DEMO_PORTS = [ // port, weight, proto
    [23, 12, 'tcp'], [22, 10, 'tcp'], [445, 7, 'tcp'], [3389, 7, 'tcp'], [80, 6, 'tcp'], [443, 4, 'tcp'],
    [8080, 4, 'tcp'], [5060, 3, 'udp'], [1900, 2, 'udp'], [123, 2, 'udp'], [7547, 2, 'tcp'], [2323, 2, 'tcp'],
    [6379, 2, 'tcp'], [5555, 2, 'tcp'], [8443, 2, 'tcp'], [3306, 1, 'tcp'], [1433, 1, 'tcp'], [9200, 1, 'tcp'],
    [27017, 1, 'tcp'], [37215, 1, 'tcp'], [53, 2, 'udp'], [161, 1, 'udp'], [11211, 1, 'udp'],
  ];
  const pick = (arr, wi) => {
    const total = arr.reduce((s, a) => s + a[wi], 0);
    let r = Math.random() * total;
    for (const a of arr) { if ((r -= a[wi]) < 0) return a; }
    return arr[arr.length - 1];
  };
  const randIp = () => `${1 + Math.floor(Math.random() * 222)}.${Math.floor(Math.random() * 256)}.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`;

  function demoEvent(src = pick(DEMO_SOURCES, 5), ip = randIp(), port) {
    let p = port || (Math.random() < 0.15 ? [1024 + Math.floor(Math.random() * 64000), 0, Math.random() < 0.5 ? 'tcp' : 'udp'] : pick(DEMO_PORTS, 1));
    if (Math.random() < 0.02) p = [null, 0, 'icmp'];
    addEvent({
      t: Date.now(), ip, proto: p[2], dport: p[0], action: 'block',
      lat: src[0] + (Math.random() - 0.5) * 3, lon: src[1] + (Math.random() - 0.5) * 3,
      city: src[2], cc: src[3], country: src[4],
    });
  }

  function startDemo() {
    setStatus('demo', 'demo');
    const tick = () => {
      if (Math.random() < 0.04) {            // a scanner sweeping several ports
        const src = pick(DEMO_SOURCES, 5), ip = randIp(), n = 5 + Math.floor(Math.random() * 12);
        for (let i = 0; i < n; i++) setTimeout(() => demoEvent(src, ip, pick(DEMO_PORTS, 1)), i * 120);
      } else demoEvent();
      setTimeout(tick, -Math.log(1 - Math.random()) * 380);
    };
    for (let i = 0; i < 25; i++) demoEvent();
    tick();
  }

  // ---------- boot ----------
  async function start() {
    let cfg = { mode: 'live', home: null, attribution: 'dbip' };
    try { Object.assign(cfg, await (await fetch('config.json')).json()); } catch { cfg.mode = 'demo'; }
    if (qs.get('demo') === '1') cfg.mode = 'demo';
    const h = qs.get('home') ? qs.get('home').split(',').map(Number) : (store.get('home', null) || cfg.home);
    if (Array.isArray(h) && h.length === 2 && h.every(Number.isFinite)) state.home = h;

    $('credit').innerHTML = cfg.mode === 'demo' ? 'Demo mode · simulated traffic'
      : cfg.attribution === 'dbip' ? '<a href="https://db-ip.com" target="_blank" rel="noopener">IP Geolocation by DB-IP</a>'
      : cfg.attribution === 'maxmind' ? 'Includes GeoLite2 data created by MaxMind, available from <a href="https://www.maxmind.com" target="_blank" rel="noopener">maxmind.com</a>'
      : '';

    await loadWorld();
    buildControls();
    render();
    requestAnimationFrame(frame);
    setInterval(updatePanel, 500);
    setInterval(feedTick, FEED_STEP_MS);
    if (cfg.mode === 'demo') startDemo(); else connectLive();
  }

  start().catch(err => { setStatus('off', 'error'); console.error(err); });
})();
