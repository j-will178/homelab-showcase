/* Space section (/space/): two flat views of api/space. Plain JS + d3 (geo only), no build step.
 *
 * Left, "where they are": looking down on the ecliptic (the plane of Earth's orbit), rotated so the
 * Sun is always to the left. Earth with Hubble's orbit (enlarged), Webb at L2 in its real direction
 * from Earth. Each telescope gets a line toward its target, shortened by how far the target sits
 * above or below the plane (▲/▼ angle), plus the zones it may point into (Hubble: not within 50° of
 * the Sun; Webb: 85–135° from it).
 * Right, "what they're looking at": the whole sky (Mollweide, RA increasing to the left), with each
 * telescope's current target, a fading trail through its recent ones, the ecliptic, the Milky Way
 * plane and the Sun's limits. Current labels always show; an older label is skipped when it would
 * overlap another (it's still listed in the cards below).
 */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const COLOR = { hst: '#2dd4bf', jwst: '#a78bfa', sun: '#fbbf24', avoid: '#f87171' };
  const REFRESH_MS = 30000;
  const D = Math.PI / 180;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // ------------------------------------------------------------------ small astronomy helpers
  const EPS = 23.4393 * D;
  function eclToEq(lon, lat) {
    lon *= D; lat *= D;
    const x = Math.cos(lat) * Math.cos(lon);
    const y = Math.cos(lat) * Math.sin(lon) * Math.cos(EPS) - Math.sin(lat) * Math.sin(EPS);
    const z = Math.cos(lat) * Math.sin(lon) * Math.sin(EPS) + Math.sin(lat) * Math.cos(EPS);
    return [((Math.atan2(y, x) / D) + 360) % 360, Math.asin(z) / D];
  }
  function galToEq(l, b) {                       // galactic -> J2000 RA/Dec
    const aG = 192.85948 * D, dG = 27.12825 * D, lN = 122.93192 * D;
    l *= D; b *= D;
    const dec = Math.asin(Math.sin(dG) * Math.sin(b) + Math.cos(dG) * Math.cos(b) * Math.cos(lN - l));
    const ra = aG + Math.atan2(Math.cos(b) * Math.sin(lN - l), Math.cos(dG) * Math.sin(b) - Math.sin(dG) * Math.cos(b) * Math.cos(lN - l));
    return [((ra / D) + 360) % 360, dec / D];
  }
  const sky = (ra, dec) => [-ra, dec];           // RA increases to the left, as on a sky chart

  // ------------------------------------------------------------------ formatting
  const hm = iso => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  function dur(s) {
    s = Math.max(0, s);
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400) return `${Math.round(s / 3600)} h`;
    return `${+(s / 86400).toFixed(1)} d`;
  }
  const until = iso => (Date.parse(iso) - Date.now()) / 1000;
  const tilt = lat => lat == null ? '' : `${lat >= 0 ? '▲' : '▼'} ${Math.abs(Math.round(lat))}°`;
  const textW = (s, px = 12) => s.length * px * 0.56 + 6;

  // ------------------------------------------------------------------ left: top-down view
  function drawTop(data) {
    const svg = d3.select('#spTop');
    svg.selectAll('*').remove();
    const W = 760, H = 470, E = [215, 215];
    const sunLon = data.sun.ecl_lon;
    const ang = lon => ((lon - sunLon + 180) % 360 + 360) % 360;       // screen angle, 0 = right, CCW
    const at = (p, a, r) => [p[0] + r * Math.cos(a * D), p[1] - r * Math.sin(a * D)];
    const wedge = (p, a0, a1, r) => {
      const [x0, y0] = at(p, a0, r), [x1, y1] = at(p, a1, r);
      return `M${p[0]},${p[1]} L${x0},${y0} A${r},${r} 0 ${(a1 - a0) % 360 > 180 ? 1 : 0} 0 ${x1},${y1} Z`;
    };
    svg.append('defs').html('<marker id="spArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5"/></marker>');

    const hst = data.telescopes.find(t => t.id === 'hst'), jwst = data.telescopes.find(t => t.id === 'jwst');
    // Webb's real direction from Earth, but its sideways swing squeezed so the drawing stays on screen
    const webbAng = jwst?.position ? ((ang(jwst.position.ecl_lon) + 180) % 360 - 180) * 0.6 : 0;
    const webbAt = at(E, webbAng, 320);
    const hubbleAt = hst?.position ? at(E, ang(hst.position.ecl_lon), 42) : null;

    // zones
    svg.append('path').attr('d', wedge(E, 180 - 50, 180 + 50, 120))
      .attr('fill', COLOR.avoid).attr('fill-opacity', 0.1).attr('stroke', COLOR.avoid).attr('stroke-width', 0.6).attr('stroke-dasharray', '4 3');
    if (jwst) for (const [a0, a1] of [[45, 95], [265, 315]]) {
      svg.append('path').attr('d', wedge(webbAt, a0, a1, 95))
        .attr('fill', COLOR.sun).attr('fill-opacity', 0.13).attr('stroke', COLOR.sun).attr('stroke-opacity', 0.6).attr('stroke-width', 0.6);
    }
    // sun direction, Earth, Hubble's orbit, Earth-Webb line
    svg.append('line').attr('x1', 95).attr('y1', E[1]).attr('x2', 22).attr('y2', E[1])
      .attr('stroke', COLOR.sun).attr('stroke-width', 1.6).attr('marker-end', 'url(#spArrow)');
    svg.append('text').attr('x', 22).attr('y', E[1] - 10).attr('class', 'lbl-muted').text('To the Sun');
    svg.append('line').attr('x1', E[0]).attr('y1', E[1]).attr('x2', webbAt[0]).attr('y2', webbAt[1])
      .attr('stroke', 'var(--border)').attr('stroke-dasharray', '3 5');
    svg.append('circle').attr('cx', E[0]).attr('cy', E[1]).attr('r', 42).attr('fill', 'none')
      .attr('stroke', COLOR.hst).attr('stroke-opacity', 0.5).attr('stroke-dasharray', '2 3');
    svg.append('circle').attr('cx', E[0]).attr('cy', E[1]).attr('r', 17).attr('fill', '#1d4f7a').attr('stroke', '#5fa8e6').attr('stroke-width', 0.8);
    // Earth's caption goes on the side Hubble's pointing line doesn't use
    const hUp = hst?.current?.ecl_lon != null && Math.sin(ang(hst.current.ecl_lon) * D) > 0;
    const capY = hUp ? E[1] + 64 : E[1] - 74;
    svg.append('text').attr('x', E[0]).attr('y', capY).attr('text-anchor', 'middle').attr('class', 'lbl-b').text('Earth');
    svg.append('text').attr('x', E[0]).attr('y', capY + 16).attr('text-anchor', 'middle').attr('class', 'lbl-muted')
      .text(hst?.position ? `Hubble orbit, enlarged · ${hst.position.alt_km} km` : 'Hubble orbit, enlarged');

    // pointing lines + labels
    const placed = [];
    function point(t, from) {
      const cur = t.current;
      const c = COLOR[t.id];
      if (cur && cur.ecl_lon != null) {
        const a = ang(cur.ecl_lon), len = Math.max(22, 115 * Math.cos(cur.ecl_lat * D));
        const tip = at(from, a, len);
        svg.append('line').attr('x1', from[0]).attr('y1', from[1]).attr('x2', tip[0]).attr('y2', tip[1])
          .attr('stroke', c).attr('stroke-width', 2.2).attr('marker-end', 'url(#spArrow)');
        const text = `${tilt(cur.ecl_lat)} · ${cur.target}`;
        const w = textW(text, 13), right = Math.cos(a * D) >= 0;
        let x = tip[0] + (right ? 8 : -8), y = tip[1] + (Math.sin(a * D) > 0 ? -6 : 14);
        if (right && x + w > W - 4) x = W - 4 - w;
        if (!right && x - w < 4) x = 4 + w;
        const anchor = right ? 'start' : 'end';
        placed.push([anchor === 'start' ? x : x - w, y - 12, w, 16]);
        svg.append('text').attr('x', x).attr('y', y).attr('text-anchor', anchor).attr('class', 'lbl-b').attr('fill', c).style('fill', c).text(text);
      }
    }
    if (hst) point(hst, hubbleAt || E);
    if (jwst) point(jwst, webbAt);
    if (hubbleAt) svg.append('circle').attr('cx', hubbleAt[0]).attr('cy', hubbleAt[1]).attr('r', 5.5).attr('fill', COLOR.hst).attr('stroke', '#0b1a19');
    if (jwst) {
      svg.append('circle').attr('cx', webbAt[0]).attr('cy', webbAt[1]).attr('r', 7).attr('fill', COLOR.jwst).attr('stroke', '#17132b');
      // the name goes on the side Webb's pointing line doesn't use
      const wUp = jwst.current?.ecl_lon != null && Math.sin(ang(jwst.current.ecl_lon) * D) > 0;
      svg.append('text').attr('x', webbAt[0]).attr('y', webbAt[1] + (wUp ? 26 : -16)).attr('text-anchor', 'middle').attr('class', 'lbl-b').text('Webb (L2)');
    }
    // distance on the Earth-Webb line: below it, or above if a target label is already there
    if (jwst?.position) {
      const text = `${(jwst.position.dist_km / 1e6).toFixed(2)} million km`, w = textW(text, 12);
      const mid = [(E[0] + webbAt[0]) / 2, (E[1] + webbAt[1]) / 2];
      const hits = y => placed.some(([bx, by, bw, bh]) => mid[0] - w / 2 < bx + bw && mid[0] + w / 2 > bx && y - 12 < by + bh && y + 4 > by);
      const y = [mid[1] + 18, mid[1] - 10, mid[1] + 34, mid[1] - 26].find(v => !hits(v)) ?? mid[1] + 18;
      svg.append('text').attr('x', mid[0]).attr('y', y).attr('text-anchor', 'middle').attr('class', 'lbl-muted').text(text);
    }
    // legend
    const L = [[COLOR.sun, 0.13, 'Where Webb can point (85–135° from the Sun; in-plane slice)'],
               [COLOR.avoid, 0.1, "Hubble's Sun avoidance (50°)"]];
    L.forEach(([c, o, s], i) => {
      svg.append('rect').attr('x', 14).attr('y', 412 + i * 18).attr('width', 14).attr('height', 10).attr('fill', c).attr('fill-opacity', o).attr('stroke', c).attr('stroke-width', 0.6);
      svg.append('text').attr('x', 36).attr('y', 421 + i * 18).attr('class', 'lbl-muted').text(s);
    });
    svg.append('text').attr('x', 14).attr('y', 457).attr('class', 'lbl-muted')
      .text('Line = current pointing, shortened by its tilt out of the plane; ▲▼ = above / below');
  }

  // ------------------------------------------------------------------ right: all-sky map
  function drawSky(data) {
    const svg = d3.select('#spSky');
    svg.selectAll('*').remove();
    const W = 760, H = 470;
    const proj = d3.geoMollweide().fitExtent([[12, 8], [W - 12, 392]], { type: 'Sphere' });
    const path = d3.geoPath(proj);
    svg.append('path').datum({ type: 'Sphere' }).attr('class', 'sphere').attr('d', path);
    svg.append('path').datum(d3.geoGraticule().step([30, 30])()).attr('class', 'grat').attr('d', path);
    const line = pts => ({ type: 'LineString', coordinates: pts });
    const ring = f => d3.range(0, 361, 2).map(f);
    svg.append('path').datum(line(ring(l => sky(...galToEq(l, 0))))).attr('class', 'gal').attr('d', path);
    svg.append('path').datum(line(ring(l => [l, 0]))).attr('class', 'eq').attr('d', path);
    svg.append('path').datum(line(ring(l => sky(...eclToEq(l, 0))))).attr('class', 'ecl').attr('d', path);

    const s = data.sun, sunC = sky(s.ra, s.dec);
    svg.append('path').datum(d3.geoCircle().center(sunC).radius(50)()).attr('d', path)
      .attr('fill', COLOR.avoid).attr('fill-opacity', 0.07).attr('stroke', COLOR.avoid).attr('stroke-width', 0.7).attr('stroke-dasharray', '4 3');
    for (const r of [85, 135]) {
      svg.append('path').datum(d3.geoCircle().center(sunC).radius(r)()).attr('d', path)
        .attr('fill', 'none').attr('stroke', COLOR.sun).attr('stroke-opacity', 0.45).attr('stroke-width', 0.7).attr('stroke-dasharray', '2 4');
    }
    const sp = proj(sunC);
    svg.append('circle').attr('cx', sp[0]).attr('cy', sp[1]).attr('r', 7).attr('fill', COLOR.sun);

    const boxes = [[sp[0] - 14, sp[1] - 10, 28, 20]];     // the Sun's own spot
    const free = b => b[0] >= 4 && b[1] >= 2 && b[0] + b[2] <= W - 4 && b[1] + b[3] <= 400 &&
      !boxes.some(o => b[0] < o[0] + o[2] && o[0] < b[0] + b[2] && b[1] < o[1] + o[3] && o[1] < b[1] + b[3]);
    function label(p, text, cls, color, opacity, force) {
      const w = textW(text, cls === 'lbl-b' ? 13 : 12), h = 15;
      const tries = [[p[0] + 10, p[1] + 4, 'start'], [p[0] - 10, p[1] + 4, 'end'], [p[0], p[1] - 10, 'middle'], [p[0], p[1] + 20, 'middle']];
      for (const [x, y, anchor] of tries) {
        const bx = anchor === 'start' ? x : anchor === 'end' ? x - w : x - w / 2;
        const b = [bx, y - 12, w, h];
        if (free(b)) {
          boxes.push(b);
          svg.append('text').attr('x', x).attr('y', y).attr('text-anchor', anchor).attr('class', cls)
            .style('fill', color).attr('opacity', opacity).text(text);
          return true;
        }
      }
      if (force) {                                        // current target: always shown, even if crowded
        const [x, y] = [p[0] + 10, p[1] + 4];
        boxes.push([x, y - 12, w, h]);
        svg.append('text').attr('x', x).attr('y', y).attr('class', cls).style('fill', color).text(text);
      }
      return false;
    }

    const tels = data.telescopes.filter(t => COLOR[t.id]);
    // trails and dots first, labels after (current ones first so they get the best spots)
    for (const t of tels) {
      const pts = [...t.recent].reverse().concat(t.current ? [t.current] : []).filter(v => v.ra != null);
      if (pts.length > 1) svg.append('path').datum(line(pts.map(v => sky(v.ra, v.dec)))).attr('d', path)
        .attr('fill', 'none').attr('stroke', COLOR[t.id]).attr('stroke-opacity', 0.45).attr('stroke-dasharray', '3 3');
      t.recent.forEach((v, i) => {
        if (v.ra == null) return;
        const p = proj(sky(v.ra, v.dec));
        svg.append('circle').attr('cx', p[0]).attr('cy', p[1]).attr('r', 3.5).attr('fill', COLOR[t.id]).attr('opacity', 0.75 - i * 0.1);
      });
      if (t.current?.ra != null) {
        const p = proj(sky(t.current.ra, t.current.dec));
        svg.append('circle').attr('cx', p[0]).attr('cy', p[1]).attr('r', 7).attr('fill', COLOR[t.id]).attr('stroke', '#0b0f17').attr('stroke-width', 1.5);
        boxes.push([p[0] - 8, p[1] - 8, 16, 16]);
      }
    }
    for (const t of tels) if (t.current?.ra != null) label(proj(sky(t.current.ra, t.current.dec)), t.current.target, 'lbl-b', COLOR[t.id], 1, true);
    const older = tels.flatMap(t => t.recent.map((v, i) => ({ t, v, i }))).filter(o => o.v.ra != null).sort((a, b) => a.i - b.i);
    for (const { t, v, i } of older) label(proj(sky(v.ra, v.dec)), `${v.target} · ${dur(v.ago_s)}`, '', COLOR[t.id], 0.85 - i * 0.1, false);

    // legend
    const y0 = 420;
    const items = [
      ['dot', COLOR.jwst, 'Webb now'], ['dot', COLOR.hst, 'Hubble now'], ['faded', '#9aa', 'earlier targets'],
      ['dash', COLOR.sun, 'ecliptic (Sun’s path)'], ['ring', COLOR.avoid, 'Hubble 50° Sun limit'], ['dots', COLOR.sun, 'Webb 85–135° band'],
    ];
    items.forEach(([kind, c, s], k) => {
      const x = 14 + (k % 3) * 250, y = y0 + Math.floor(k / 3) * 20;
      if (kind === 'dot' || kind === 'faded') svg.append('circle').attr('cx', x + 6).attr('cy', y - 4).attr('r', kind === 'dot' ? 5 : 3.5).attr('fill', c).attr('opacity', kind === 'dot' ? 1 : 0.5);
      else svg.append('line').attr('x1', x).attr('y1', y - 4).attr('x2', x + 14).attr('y2', y - 4).attr('stroke', c)
        .attr('stroke-dasharray', kind === 'dash' ? '6 4' : kind === 'ring' ? '4 3' : '2 4').attr('stroke-width', 1.2);
      svg.append('text').attr('x', x + 20).attr('y', y).attr('class', 'lbl-muted').text(s);
    });
  }

  // ------------------------------------------------------------------ info panel
  // Which visit each card's panel shows: the current one, unless a row was clicked (kept until
  // 2 minutes without a click, then back to current so the kiosk never sticks on an old target).
  const sel = {};
  let lastClick = 0;
  const vkey = v => `${v.target}|${v.begin}`;
  const NAME = { hst: 'Hubble', jwst: 'Webb' };

  function placeholder(c) {
    return `<svg viewBox="0 0 132 132" aria-hidden="true"><rect width="132" height="132" fill="none"/>` +
      ['M66 10V26', 'M66 106V122', 'M10 66H26', 'M106 66H122'].map(d => `<path d="${d}" stroke="${c}" stroke-width="1.2"/>`).join('') +
      `<text x="66" y="70" text-anchor="middle" class="ph">no image yet</text></svg>`;
  }

  function infoPanel(t, v, isCurrent) {
    const c = COLOR[t.id];
    const f = v.info;
    const img = f?.image
      ? `<img src="${esc(f.image)}" alt="Sky survey image around ${esc(v.target)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{innerHTML:${esc(JSON.stringify(placeholder(c)))}}))">`
      : placeholder(c);
    const lines = [];
    if (!isCurrent) lines.push(`<p class="sel-note">Showing <b>${esc(v.target)}</b> · ${v.ago_s != null ? `${dur(v.ago_s)} ago` : `in ${dur(until(v.begin))}`}</p>`);
    if (!f || f.pending) lines.push('<p class="fact muted">Looking this target up…</p>');
    if (f) {
      const badges = [f.type, f.morph].filter(Boolean).map((b, i) => `<span class="badge${i ? ' dim' : ''}" style="--tc:${c}">${esc(b)}</span>`);
      if (f.resolved === false) badges.push('<span class="badge dim">Survey field · no catalog match</span>');
      if (v.category === 'Calibration' || /calibration|wavefront/i.test(v.visit_type || '')) badges.push(`<span class="badge dim">${esc(v.visit_type || 'Calibration')}</span>`);
      if (badges.length) lines.push(`<p>${badges.join('')}</p>`);
      const facts = [f.distance && `<b>${esc(f.distance)}</b>`, f.aliases?.length && `also called ${esc(f.aliases.join(', '))}`].filter(Boolean);
      if (facts.length) lines.push(`<p class="fact">${facts.join(' · ')}</p>`);
      if (f.wiki?.summary) lines.push(`<p class="sum">${esc(f.wiki.summary)}${f.wiki.url ? ` <a href="${esc(f.wiki.url)}" target="_blank" rel="noopener">Wikipedia</a>` : ''}</p>`);
      if (f.nearest) lines.push(`<p class="fact"><b>Nearest known object:</b> ${esc(f.nearest.name)}${f.nearest.type ? ` (${esc(f.nearest.type.toLowerCase())})` : ''}, ${f.nearest.sep_arcmin}′ away${f.nearest.distance ? ` · ${esc(f.nearest.distance)}` : ''}</p>`);
      const p = f.program;
      if (p) lines.push(`<p class="fact"><b>Why:</b> <a href="${esc(p.url)}" target="_blank" rel="noopener">program ${esc(p.id)}</a>` +
        `${p.title ? ` · “${esc(p.title)}”` : ''}${p.pi ? ` · PI ${esc(p.pi)}` : ''}${p.category ? ` · ${esc(p.category)}` : ''}</p>`);
      if (f.sources?.length) lines.push(`<p class="src">${esc(f.sources.join(' · '))}</p>`);
    }
    const cap = f?.image_caption;
    const capText = cap ? (cap.kind === 'telescope' ? cap.text : `${cap.text} · not ${NAME[t.id] || t.name}'s own`)
      : (f?.image ? 'Sky survey image' : 'No image of this spot yet');
    return `<div class="info"><figure>${img}<figcaption>${esc(capText)}</figcaption></figure>` +
      `<div>${lines.join('')}</div></div>`;
  }

  // ------------------------------------------------------------------ cards + chips
  function card(t) {
    const p = t.position;
    const pos = !p ? 'position unavailable' : p.kind === 'l2'
      ? `L2 · ${(p.dist_km / 1e6).toFixed(2)} million km`
      : `${p.alt_km} km up · ${Math.round(p.period_min)}-min orbit`;
    const c = t.current;
    const head = c
      ? `<div class="now">${esc(c.target)}</div><div class="meta">${esc(c.instrument || 'instrument not listed')} · until ${hm(c.end)} (${dur(until(c.end))})` +
        `${c.sun_angle != null ? ` · ${Math.round(c.sun_angle)}° from the Sun` : ' · no coordinates published'}${c.proposal ? ` · program ${esc(c.proposal)}` : ''}</div>`
      : `<div class="now">Between targets</div><div class="meta">${t.schedule_covers_now ? 'slewing or calibrating' : 'no published schedule covers right now'}</div>`;
    if (Date.now() - lastClick > 120000) delete sel[t.id];
    const all = [...(c ? [c] : []), ...t.next, ...t.recent];
    const shown = all.find(v => vkey(v) === sel[t.id]) || c || t.next[0] || null;
    const row = (v, cls, label, when) => `<li class="${cls}${shown && vkey(v) === vkey(shown) ? ' on' : ''}" data-tel="${t.id}" data-key="${esc(vkey(v))}" tabindex="0">` +
      `<span class="t">${label}</span><span class="w">${when}</span></li>`;
    const rows = (c ? [row(c, 'now-row', `now: ${esc(c.target)}`, `until ${hm(c.end)}`)] : [])
      .concat(t.next.map(v => row(v, 'next', `next: ${esc(v.target)}`, `in ${dur(until(v.begin))}`)))
      .concat(t.recent.map(v => row(v, '', `${esc(v.target)}${v.instrument ? ` <span class="w">· ${esc(v.instrument)}</span>` : ''}`, `${dur(v.ago_s)} ago`)));
    return `<article class="sp-card" style="--tc:${COLOR[t.id] || 'var(--accent)'}"><h2>${esc(t.name)}<span class="pos">${esc(pos)}</span></h2>${head}` +
      (shown ? infoPanel(t, shown, shown === c) : '') +
      (rows.length ? `<ol>${rows.join('')}</ol>` : '<p class="none">no schedule data yet</p>') + '</article>';
  }

  function chips(data) {
    const out = [];
    if (data.mode === 'demo') out.push('<span class="chip warn">simulated data</span>');
    const via = [...new Set(Object.entries(data.sources || {}).filter(([k, s]) => k.startsWith('schedule:') && s.via).map(([, s]) => s.via))];
    out.push(`<span class="chip">planned schedules · ${via.length ? via.map(v => v === 'across' ? 'NASA ACROSS' : 'STScI Space Telescope Live').join(' + ') : 'STScI / NASA'}</span>`);
    for (const [k, s] of Object.entries(data.sources || {})) if (s.error) out.push(`<span class="chip warn" title="${esc(s.error)}">${esc(k)}: unavailable</span>`);
    out.push(`<span class="chip">updated ${hm(data.generated_at)}</span>`);
    $('spChips').innerHTML = out.join('');
  }

  const cardsHtml = data => ['jwst', 'hst'].map(id => data.telescopes.find(t => t.id === id)).filter(Boolean).map(card).join('');

  async function refresh() {
    try {
      const r = await fetch('api/space', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      chips(data);
      drawTop(data);
      drawSky(data);
      lastData = data;
      $('spCards').innerHTML = cardsHtml(data);
    } catch (e) {
      console.warn('space: fetch failed', e);
      $('spChips').innerHTML = '<span class="chip warn">space data unavailable</span>';
    }
  }

  let lastData = null;
  function pick(li) {
    if (!li || !lastData) return;
    sel[li.dataset.tel] = li.dataset.key;
    lastClick = Date.now();
    $('spCards').innerHTML = cardsHtml(lastData);
  }
  $('spCards').addEventListener('click', e => pick(e.target.closest('li[data-key]')));
  $('spCards').addEventListener('keydown', e => { if (e.key === 'Enter') pick(e.target.closest('li[data-key]')); });

  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === 'Home') location.href = 'home/';
    else if (e.key === 'r' || e.key === 'R') location.href = 'rack/';
    else if (e.key === 'g' || e.key === 'G') location.href = 'space/gallery/';
  });
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
