/* Pink Cloud - OCR processing overlay: shader D ("3D glyph swarm").
   Ported from design-drafts/pink-cloud-loading-shader-D.html (removed; git history 3c53e7f) (reference
   cosmos.so/e/933016892). Canvas 2D, no dependencies.

   While a job runs, the page being read is cut into a glyph grid and wrapped
   around a globe; the globe unrolls into a tilted 3D page and an orange scan
   glow moves down it with the real job progress, turning each dark dot
   (unread) into an orange plus (read). On completion it finishes the scan,
   shows "Page read" and fades out.

   API (window.PCLoader), driven by upload.js:
     show()                 open the overlay (no-op when already open)
     setImage(src)          File/Blob, URL or <img>; null or an image the
                            browser can't decode (PDF, TIFF) -> sample page
     setProgress(0..1)      real progress; the scan follows it smoothly
     setLabel(text, sub?)   pill text and the small line under it
     done(text?)            finish the scan, then fade out
     hide()                 close at once
     onDismiss = fn         called when the user presses "Hide" or Esc
   Reduced motion: no globe or rocking; a still page whose scan steps with
   progress. ?loader=0 on the page turns the overlay off. */
(function () {
  'use strict';
  if (window.PCLoader) return;
  var off = new URLSearchParams(location.search).get('loader') === '0';
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var COLS = 27, ROWS = 36;

  var root, cv, ctx, pctEl, lblEl, subEl, hideBtn, C, raf = 0;
  var W = 0, H = 0, DPR = 1;
  var pts = [], open = false, finishing = false, finishedAt = 0, t0 = 0;
  var target = 0, shown = 0, pctShown = 0, lastNow = 0, imgToken = 0, api;

  function tok(name, fb) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    var m = /^#([0-9a-f]{6})$/i.exec(v || fb) || /^#([0-9a-f]{6})$/i.exec(fb);
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function raw(name, fb) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb; }
  function mount() {
    if (root) return;
    C = {
      bg: tok('--pc-color-bg-canvas', '#FDFCFB'),
      ink: tok('--pc-color-text-primary', '#1E1A18'),
      dk: tok('--pc-color-primary-active', '#B32E08'),
      or: tok('--pc-color-primary', '#F94612'),
      or2: tok('--pc-color-primary-gradient-end', '#FA683D'),
      soft: tok('--pc-color-primary-soft-border', '#FFC9B3'),
      card: tok('--pc-color-bg-surface', '#FFFFFF'),
      line: tok('--pc-color-border-subtle', '#E6E2DE'),
      shadow: raw('--pc-loader-card-shadow-color', '#1E1A181F'),
      shadowBlur: parseFloat(raw('--pc-loader-card-shadow-blur', '14')) || 14,
      shadowY: parseFloat(raw('--pc-loader-card-shadow-y', '6')) || 6
    };
    root = document.createElement('div');
    root.className = 'pc-loader';
    root.setAttribute('role', 'status');
    root.setAttribute('aria-live', 'polite');
    cv = document.createElement('canvas');
    cv.setAttribute('aria-hidden', 'true');
    ctx = cv.getContext('2d');
    var pill = document.createElement('div');
    pill.className = 'pc-loader-pill';
    lblEl = document.createElement('span');
    lblEl.textContent = 'Reading page';
    pctEl = document.createElement('span');
    pctEl.className = 'pc-loader-pct';
    pctEl.textContent = '0%';
    pill.appendChild(lblEl); pill.appendChild(pctEl);
    subEl = document.createElement('div');
    subEl.className = 'pc-loader-sub';
    hideBtn = document.createElement('button');
    hideBtn.type = 'button';
    hideBtn.className = 'pc-loader-hide';
    hideBtn.textContent = 'Hide';
    hideBtn.title = 'Keep working; the file list still shows progress';
    hideBtn.addEventListener('click', dismiss);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && open) dismiss(); });
    root.appendChild(cv); root.appendChild(pill); root.appendChild(subEl); root.appendChild(hideBtn);
    document.body.appendChild(root);
    addEventListener('resize', size);
    build(defaultPage());
  }
  function dismiss() { hide(); if (api.onDismiss) api.onDismiss(); }

  /* ---------- default page: a Tamil document (used when no image) ---------- */
  function defaultPage() {
    var o = document.createElement('canvas'); o.width = 300; o.height = 400;
    var g = o.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, 300, 400); g.fillStyle = '#111';
    g.fillRect(20, 22, 150, 20); g.fillRect(200, 22, 80, 78);
    g.font = 'bold 18px "Noto Sans Tamil", sans-serif'; g.fillText('தமிழ் செய்தி', 22, 68);
    var seed = 7, rnd = function () { return (seed = (seed * 16807) % 2147483647) / 2147483647; };
    for (var y = 112; y < 380; y += 21) {
      var x = 20;
      while (x < 260) { var w = 18 + rnd() * 46; if (x + w > 280) break; g.fillRect(x, y, w, 9); x += w + 8 + rnd() * 5; }
      if (rnd() < .18) y += 8;
    }
    return o;
  }

  /* ---------- sample an image into glyph cells ---------- */
  function build(src) {
    var s = document.createElement('canvas'); s.width = COLS; s.height = ROWS;
    var g = s.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, COLS, ROWS);
    var iw = src.naturalWidth || src.width, ih = src.naturalHeight || src.height;
    if (!iw || !ih) return false;
    var k = Math.min(COLS / iw, ROWS / ih);                     // contain: the whole page shows
    g.imageSmoothingQuality = 'high';
    g.drawImage(src, (COLS - iw * k) / 2, (ROWS - ih * k) / 2, iw * k, ih * k);
    var d;
    try { d = g.getImageData(0, 0, COLS, ROWS).data; } catch (e) { return false; } // tainted
    var L = [], mean = 0, i;
    for (i = 0; i < COLS * ROWS; i++) { var l = (.299 * d[i * 4] + .587 * d[i * 4 + 1] + .114 * d[i * 4 + 2]) / 255; L.push(l); mean += l; }
    mean /= L.length;
    var out = [];
    for (var r = 0; r < ROWS; r++) for (var c = 0; c < COLS; c++) {
      var dark = Math.min(1, Math.max(0, (mean - L[r * COLS + c] + .06) / .45));
      if (dark < .12) continue;
      var lon = ((c + .5) / COLS - .5) * Math.PI * 2, lat = ((ROWS - 1) / 2 - r) / (ROWS - 1) * Math.PI * .86;
      out.push({
        c: c, r: r, w: dark, plus: dark > .8,
        sx: Math.sin(lon) * Math.cos(lat), sy: Math.sin(lat), sz: Math.cos(lon) * Math.cos(lat),
        px: (c - (COLS - 1) / 2) / (ROWS - 1) * 1.5, py: ((r - (ROWS - 1) / 2) / (ROWS - 1)) * -2,
        delay: (r / (ROWS - 1)) * .5 + Math.abs(c - (COLS - 1) / 2) / COLS * .18
      });
    }
    if (out.length < 12) return false;                          // blank image: keep the current page
    pts = out;
    return true;
  }
  function setImage(src) {
    mount();
    var my = ++imgToken;
    if (!src) { build(defaultPage()); return; }
    if (src instanceof HTMLImageElement || src instanceof HTMLCanvasElement) { if (!build(src)) build(defaultPage()); return; }
    var url = src, revoke = false;
    if (typeof Blob !== 'undefined' && src instanceof Blob) {
      if (!/^image\/(png|jpe?g|webp|gif|bmp)$/i.test(src.type || '')) { build(defaultPage()); return; }
      url = URL.createObjectURL(src); revoke = true;
    }
    var im = new Image();
    im.onload = function () { if (my === imgToken && !build(im)) build(defaultPage()); if (revoke) URL.revokeObjectURL(url); };
    im.onerror = function () { if (my === imgToken) build(defaultPage()); if (revoke) URL.revokeObjectURL(url); };
    im.src = url;
  }

  /* ---------- easing ---------- */
  function clamp(v, a, b) { a = a === undefined ? 0 : a; b = b === undefined ? 1 : b; return Math.min(b, Math.max(a, v)); }
  function backOut(t) { var s = 1.9; t -= 1; return t * t * ((s + 1) * t + s) + 1; }
  function inOut(t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
  function mix(a, b, t) { return a + (b - a) * t; }
  function col(a, b, t, al) { return 'rgba(' + (mix(a[0], b[0], t) | 0) + ',' + (mix(a[1], b[1], t) | 0) + ',' + (mix(a[2], b[2], t) | 0) + ',' + al + ')'; }
  function rgba(a, al) { return 'rgba(' + a[0] + ',' + a[1] + ',' + a[2] + ',' + al + ')'; }

  function size() {
    if (!cv) return;
    DPR = Math.min(window.devicePixelRatio || 1, 2); W = innerWidth; H = innerHeight;
    cv.width = W * DPR; cv.height = H * DPR; ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  /* ---------- one frame ---------- */
  var INTRO = 2.2;                                                // globe spin + unroll, seconds
  function draw(T, scan) {
    if (W !== innerWidth || H !== innerHeight) size();
    var form = reduce ? 1 : clamp((T - .5) / (INTRO - .5));
    var spin = reduce ? 0 : Math.PI * 2 * inOut(clamp(T / INTRO));
    var rock = reduce ? 0.18 : Math.sin(T * 0.9) * 0.3 + (1 - form) * 0;
    var tilt = reduce ? 0.3 : 0.30 + Math.sin(T * 0.6) * 0.06;
    var S = Math.min(W * .8, H * .86) * .36, cx = W / 2, cy = H * .44;
    var cell = S * 2 / (ROWS - 1), cr = Math.cos(rock), sr = Math.sin(rock), ct = Math.cos(tilt), st = Math.sin(tilt);

    ctx.fillStyle = rgba(C.bg, 1); ctx.fillRect(0, 0, W, H);
    var q = null;

    // page card
    if (form > 0) {
      var sh = inOut(clamp(form * 1.3 - .3));
      var proj = function (x, y) {
        var x1 = x * cr, z1 = -x * sr, y1 = y * ct - z1 * st, z2 = y * st + z1 * ct, pp = 3.2 / (3.2 - z2);
        return [cx + x1 * S * pp, cy - y1 * S * pp];
      };
      var m = .1, hw = ((COLS - 1) / 2) / (ROWS - 1) * 1.5 + m, hh = 1 + m;
      q = [proj(-hw, hh), proj(hw, hh), proj(hw, -hh), proj(-hw, -hh)];
      ctx.save(); ctx.globalAlpha = sh;
      ctx.shadowColor = C.shadow; ctx.shadowBlur = C.shadowBlur; ctx.shadowOffsetY = C.shadowY;   // gray, tight (was orange, blur 40)
      ctx.fillStyle = rgba(C.card, 1); ctx.beginPath(); ctx.moveTo(q[0][0], q[0][1]);
      for (var qi = 1; qi < 4; qi++) ctx.lineTo(q[qi][0], q[qi][1]);
      ctx.closePath(); ctx.fill(); ctx.shadowColor = 'transparent';
      ctx.strokeStyle = rgba(C.line, 1); ctx.lineWidth = 1; ctx.stroke(); ctx.restore();
    }

    var list = [], sp = Math.cos(spin), ss = Math.sin(spin), gt = .22, cg = Math.cos(gt), sg = Math.sin(gt);
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      var f = clamp((form * 1.5 - p.delay) / 1.0); f = reduce ? 1 : backOut(f);
      var gx = p.sx * sp + p.sz * ss, gz = -p.sx * ss + p.sz * sp, gy = p.sy;
      var gy2 = gy * cg - gz * sg, gz2 = gy * sg + gz * cg;
      var x1 = p.px * cr, z1 = -p.px * sr;
      var y1 = p.py * ct - z1 * st, z2 = p.py * st + z1 * ct;
      if (!reduce) z2 += 0.04 * Math.sin(p.c * .5 + p.r * .35 - T * 2.2) * f;
      var X = mix(gx * 1.05, x1, f), Y = mix(gy2 * 1.05, y1, f), Z = mix(gz2 * 1.05, z2, f);
      var persp = 3.2 / (3.2 - Z);
      var beamD = p.r / (ROWS - 1) - scan * 1.04;
      var landed = f > .9;
      list.push({ X: cx + X * S * persp, Y: cy - Y * S * persp, Z: Z, persp: persp, w: p.w, f: f, plus: p.plus,
        read: landed && beamD < 0 ? 1 : 0, beam: landed && scan > 0 && scan < 1 ? Math.exp(-Math.pow(beamD * 9, 2)) : 0 });
    }
    list.sort(function (a, b) { return a.Z - b.Z; });

    // scan glow, centred on the glyphs the beam is crossing
    if (form > .95 && scan > 0 && scan < 1) {
      var bx = 0, by = 0, n = 0;
      for (var j = 0; j < list.length; j++) if (list[j].beam > .5) { bx += list[j].X; by += list[j].Y; n++; }
      if (n) {
        ctx.save();
        // keep the beam on the page: clipped to the card, no orange haze spilling onto the background
        if (q) { ctx.beginPath(); ctx.moveTo(q[0][0], q[0][1]); for (var qc = 1; qc < 4; qc++) ctx.lineTo(q[qc][0], q[qc][1]); ctx.closePath(); ctx.clip(); }
        ctx.translate(bx / n, by / n); ctx.scale(1, .16);
        var bg = ctx.createRadialGradient(0, 0, 0, 0, 0, S * 1.05);
        bg.addColorStop(0, rgba(C.or2, .32)); bg.addColorStop(.6, rgba(C.soft, .22)); bg.addColorStop(1, rgba(C.soft, 0));
        ctx.fillStyle = bg; ctx.beginPath(); ctx.arc(0, 0, S * 1.05, 0, 6.2832); ctx.fill(); ctx.restore();
      }
    }

    ctx.lineCap = 'round';
    for (var k = 0; k < list.length; k++) {
      var g = list[k];
      var globeA = clamp((g.Z + .05) * 3.2);                     // globe: front half only
      var al = mix(globeA, 1, clamp(g.f)) * mix(.72, 1, g.w);
      if (al < .02) continue;
      var sz = cell * g.persp, globePlus = g.plus && g.f < .5;
      if (g.read || g.beam > .4 || globePlus) {
        var t = clamp(g.beam);
        ctx.strokeStyle = globePlus && !g.read ? col(C.dk, C.or, g.f * 2, al) : col(C.or, C.or2, t, al);
        ctx.lineWidth = Math.max(2, sz * .2);
        var a = sz * mix(.36, .48, t);
        ctx.beginPath(); ctx.moveTo(g.X - a, g.Y); ctx.lineTo(g.X + a, g.Y); ctx.moveTo(g.X, g.Y - a); ctx.lineTo(g.X, g.Y + a); ctx.stroke();
      } else {
        ctx.fillStyle = col(C.ink, C.dk, .45 + clamp(g.f) * .4, al * .95);
        ctx.beginPath(); ctx.arc(g.X, g.Y, Math.max(3, sz * mix(.30, .38, g.w)), 0, 6.2832); ctx.fill();
      }
    }
  }

  function frame(now) {
    raf = 0;
    if (!open) return;
    var dt = Math.min(.1, (now - (lastNow || now)) / 1000); lastNow = now;
    var T = (now - t0) / 1000;
    // the scan waits for the page to land, then follows real progress
    var goal = T < INTRO && !reduce ? 0 : target;
    shown = reduce ? goal : shown + (goal - shown) * Math.min(1, dt * 3.2);
    if (goal >= 1 && shown > .995) shown = 1;
    draw(T, shown);
    pctShown = reduce ? target : Math.max(pctShown, pctShown + (target - pctShown) * Math.min(1, dt * 4));
    if (target >= 1 && pctShown > .995) pctShown = 1;
    pctEl.textContent = Math.round(pctShown * 100) + '%';
    if (finishing && shown >= 1) {
      if (!finishedAt) finishedAt = now;
      if (now - finishedAt > 650) { hide(); return; }
    }
    if (reduce && !finishing) { raf = 0; return; }           // still frame; redrawn on each update
    raf = requestAnimationFrame(frame);
  }
  function kick() { if (open && !raf) raf = requestAnimationFrame(frame); }

  function show() {
    if (off) return;
    mount();
    if (open) return;
    open = true; finishing = false; finishedAt = 0; target = 0; shown = 0; pctShown = 0; lastNow = 0;
    t0 = performance.now();
    size();
    lblEl.textContent = 'Opening page'; subEl.textContent = '';
    root.classList.add('is-on');
    kick();
  }
  function hide() {
    open = false; finishing = false;
    if (raf) cancelAnimationFrame(raf); raf = 0;
    if (root) root.classList.remove('is-on');
  }
  function setProgress(v) { target = Math.max(target, clamp(+v || 0)); if (reduce) kick(); }
  function setLabel(t, sub) { mount(); if (t) lblEl.textContent = t; if (sub !== undefined) subEl.textContent = sub || ''; }
  function done(text) {
    if (!open) return;
    finishing = true; target = 1;
    lblEl.textContent = text || 'Page read';
    if (reduce) { hide(); return; }
    kick();
  }

  api = window.PCLoader = {
    show: show, hide: hide, setImage: setImage, setProgress: setProgress, setLabel: setLabel, done: done,
    isOpen: function () { return open; }, disabled: off, onDismiss: null
  };
})();
