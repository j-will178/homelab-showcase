/* Rack Dashboard - halftone wave transition between screens.
 *
 * A band of dots sweeps across the screen. Each dot sits on a moving height
 * field (traveling sine waves + 2-octave gradient noise); its size and
 * brightness come from the surface's slope against a light from the upper
 * right, so crests read as lit and troughs fall into shadow. The band's
 * leading edge is wavy and the dots shrink to nothing at the edges, so it
 * wipes in and out like halftone print. The screen switches underneath at
 * full cover.
 *
 *   window.waveTransition(onMidpoint) -> Promise   (resolves when finished)
 *   ?transition=wave|fade|none  (default wave; 'fade' when the OS asks for reduced motion)
 */
(() => {
  'use strict';

  const DURATION = 2200;      // ms, whole transition
  const FOG_FADE = 0.38;      // fog fades in over the first 38% and out over the last 38%
                              // (fully opaque around the switch at 50%); dots never fade
  const FOG_EDGE = 0.36;      // fog edge width (fraction of screen width): wider than the
                              // dots' edge, so fog drifts in ahead of them and lingers behind
  const FOG_CELL = 8;         // fog is computed on a coarse grid and scaled up (soft + cheap)
  const SPACING = 18;         // px between dots
  const MAX_R = SPACING * 0.26;
  const EDGE = 0.16;          // width of the soft edge, as a fraction of screen width
  const OVERSCAN = SPACING * 4; // grid extends past every screen edge: dots ride up/down
                                // with the wave by ~1.5 spacings, so this keeps edges filled
  const BG = '#070b14';

  // ---- gradient noise (classic 2D Perlin, 256 permutation) ----------------
  const perm = new Uint8Array(512);
  {
    const p = Array.from({ length: 256 }, (_, i) => i);
    let seed = 1337;
    const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
    for (let i = 255; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [p[i], p[j]] = [p[j], p[i]]; }
    for (let i = 0; i < 512; i++) perm[i] = p[i & 255];
  }
  const fade = t => t * t * t * (t * (t * 6 - 15) + 10);
  function grad(h, x, y) {
    switch (h & 7) {
      case 0: return x + y; case 1: return -x + y; case 2: return x - y; case 3: return -x - y;
      case 4: return x; case 5: return -x; case 6: return y; default: return -y;
    }
  }
  function noise(x, y) {
    const X = Math.floor(x) & 255, Y = Math.floor(y) & 255;
    x -= Math.floor(x); y -= Math.floor(y);
    const u = fade(x), v = fade(y);
    const a = perm[X] + Y, b = perm[X + 1] + Y;
    const l1 = grad(perm[a], x, y) + u * (grad(perm[b], x - 1, y) - grad(perm[a], x, y));
    const l2 = grad(perm[a + 1], x, y - 1) + u * (grad(perm[b + 1], x - 1, y - 1) - grad(perm[a + 1], x, y - 1));
    return l1 + v * (l2 - l1);   // ~[-1, 1]
  }

  // Height of the "cloth" at screen point (x, y) and time t (seconds).
  function height(x, y, t, W) {
    const s = 1 / W;
    return 0.55 * Math.sin((x * 2.2 + y * 1.1) * s * Math.PI * 2 - t * 2.4)
         + 0.30 * Math.sin((x * -0.9 + y * 2.6) * s * Math.PI * 2 + t * 1.7)
         + 0.45 * noise(x * s * 3.0 + t * 0.35, y * s * 3.0)
         + 0.20 * noise(x * s * 7.0 - t * 0.5, y * s * 7.0 + 11.3);
  }

  const smooth = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
  const easeSine = t => 0.5 - 0.5 * Math.cos(Math.PI * t);
  const easeInOut = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

  // ---- canvas -------------------------------------------------------------
  let canvas, ctx, running = null;
  let fogCanvas = null, fogCtx = null, fogImg = null;
  function ensureCanvas() {
    if (canvas) return;
    canvas = document.createElement('canvas');
    canvas.id = 'waveFx';
    canvas.setAttribute('aria-hidden', 'true');
    document.body.appendChild(canvas);
    ctx = canvas.getContext('2d');
  }

  const BUCKETS = 12;   // dots are batched by brightness: one fill() per bucket
  const palette = Array.from({ length: BUCKETS }, (_, i) => {
    const k = i / (BUCKETS - 1);                 // 0 = shadow, 1 = highlight
    const r = Math.round(28 + k * 140), g = Math.round(50 + k * 135), b = Math.round(100 + k * 110);
    return `rgb(${r},${g},${b})`;
  });

  function drawFrame(p, tSec) {
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    // Band position. Front edge sweeps across in the first half, back edge in
    // the second. It starts fully off-screen (left) and ends fully off-screen
    // (right), with room for the wide fog edge on both sides.
    // Front: fog starts entering at the left edge immediately (no dead time)
    // and ends far enough right that, even with the wavy edge (WOB), the screen
    // is solid at the switch. Back: starts far enough left for the same reason
    // and ends past the right edge so the dots (which never fade) leave cleanly.
    const a = Math.min(1, p / 0.5), b = Math.max(0, (p - 0.5) / 0.5);
    const WOB = 0.1;
    const front = W * (1 + FOG_EDGE + WOB) * easeSine(a);
    const back = W * (-(FOG_EDGE + WOB) + (1 + FOG_EDGE + 2 * WOB) * easeSine(b));
    const wob = y => W * 0.05 * Math.sin(y / H * Math.PI * 2.2 + tSec * 3) + W * 0.035 * noise(y / H * 3, tSec * 0.8);

    // ---- fog: a soft, cloudy dark veil that hides the old screen ----
    // Computed per coarse cell and scaled up with smoothing, so edges are
    // naturally soft. Wispy noise only affects the partly-covered edges; at
    // full cover it's solid. It alone fades in and out over time.
    const fogAlpha = smooth(0, FOG_FADE, p) * (1 - smooth(1 - FOG_FADE, 1, p));
    const fw = Math.ceil(W / FOG_CELL), fh = Math.ceil(H / FOG_CELL);
    if (!fogCanvas || fogCanvas.width !== fw || fogCanvas.height !== fh) {
      fogCanvas = document.createElement('canvas');
      fogCanvas.width = fw; fogCanvas.height = fh;
      fogCtx = fogCanvas.getContext('2d');
      fogImg = fogCtx.createImageData(fw, fh);
    }
    const fd = fogImg.data, fe = FOG_EDGE * W;
    for (let j = 0; j < fh; j++) {
      const y = (j + 0.5) * FOG_CELL, w = wob(y);
      for (let i = 0; i < fw; i++) {
        const x = (i + 0.5) * FOG_CELL;
        const cov = smooth(front + w, front + w - fe, x) * smooth(back + w, back + w + fe, x);
        let al = 0;
        if (cov > 0) {
          const n = noise(x / W * 3.2 - tSec * 0.55, y / H * 3.2) + 0.5 * noise(x / W * 8 + 4.1, y / H * 8 - tSec * 0.4);
          al = Math.min(1, Math.max(0, cov + cov * (1 - cov) * n * 1.6));
        }
        const k = (j * fw + i) * 4;
        fd[k] = 7; fd[k + 1] = 11; fd[k + 2] = 20; fd[k + 3] = al * fogAlpha * 255;
      }
    }
    fogCtx.putImageData(fogImg, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(fogCanvas, 0, 0, fw * FOG_CELL, fh * FOG_CELL);

    // Dots, batched by brightness.
    const paths = Array.from({ length: BUCKETS }, () => new Path2D());
    const counts = new Uint16Array(BUCKETS);
    const L = [0.55, -0.62, 0.56];              // light: from the upper right, slightly in front
    const e = 2;                                  // finite-difference step for the slope
    for (let y = -OVERSCAN; y < H + OVERSCAN; y += SPACING) {
      const w = wob(y);
      const xStart = Math.max(-OVERSCAN, back - EDGE * W + w), xEnd = Math.min(W + OVERSCAN, front + EDGE * W + w);
      const row = Math.round((y + OVERSCAN) / SPACING);
      for (let x = -OVERSCAN + (row & 1) * SPACING / 2; x < xEnd; x += SPACING) {
        if (x < xStart) continue;
        // coverage: 0 outside the band, 1 inside, smooth at both edges
        const cov = smooth(front + w, front + w - EDGE * W, x) * smooth(back + w, back + w + EDGE * W, x);
        if (cov <= 0.02) continue;
        const h = height(x, y, tSec, W);
        const dx = height(x + e * SPACING, y, tSec, W) - h, dy = height(x, y + e * SPACING, tSec, W) - h;
        // surface normal of z = h * amplitude
        const amp = 9, nx = -dx * amp, ny = -dy * amp, nz = 1, nl = Math.hypot(nx, ny, nz);
        let light = (nx * L[0] + ny * L[1] + nz * L[2]) / nl;           // -1..1
        light = Math.min(1, Math.max(0, light * 0.9 + h * 0.25 + 0.05)); // crests a touch brighter
        const r = MAX_R * cov * (0.18 + 0.82 * light);
        if (r < 0.35) continue;
        const yy = y + h * SPACING * 0.7;                                // dots ride the surface
        if (yy < -r || yy > H + r || x < -r || x > W + r) continue;      // off-screen
        const bi = Math.min(BUCKETS - 1, Math.floor(light * BUCKETS));
        paths[bi].moveTo(x + r, yy);
        paths[bi].arc(x, yy, r, 0, Math.PI * 2);
        counts[bi]++;
      }
    }
    for (let i = 0; i < BUCKETS; i++) {
      if (!counts[i]) continue;
      ctx.fillStyle = palette[i];
      ctx.fill(paths[i]);
    }
  }

  function mode() {
    const q = new URLSearchParams(location.search).get('transition');
    if (q === 'none' || q === 'fade' || q === 'wave') return q;
    return matchMedia('(prefers-reduced-motion: reduce)').matches ? 'fade' : 'wave';
  }

  /** Run the wave. onMidpoint() is called once the old screen is fully covered. */
  window.waveTransition = function (onMidpoint) {
    const m = mode();
    if (m !== 'wave') { onMidpoint(); return Promise.resolve(); }
    if (running) {          // already animating: the midpoint picks up the latest target
      running.onMidpoint = onMidpoint;
      return running.done;
    }
    ensureCanvas();
    canvas.width = innerWidth; canvas.height = innerHeight;   // 1:1 pixels, cheap on the OptiPlex iGPU
    canvas.classList.add('on');
    const state = { onMidpoint, switched: false };
    state.done = new Promise(resolve => {
      const t0 = performance.now();
      const tick = now => {
        const p = Math.min(1, (now - t0) / DURATION);
        drawFrame(p, (now - t0) / 1000);
        if (!state.switched && p >= 0.5) { state.switched = true; state.onMidpoint(); }
        if (p < 1) requestAnimationFrame(tick);
        else { canvas.classList.remove('on'); ctx.clearRect(0, 0, canvas.width, canvas.height); running = null; resolve(); }
      };
      requestAnimationFrame(tick);
    });
    running = state;
    return state.done;
  };

  // For previews/tests: draw a single frame at progress p (0..1).
  window.waveTransition.frame = (p, tSec = p * DURATION / 1000) => {
    ensureCanvas();
    canvas.width = innerWidth; canvas.height = innerHeight;
    canvas.classList.add('on');
    drawFrame(p, tSec);
  };
})();
