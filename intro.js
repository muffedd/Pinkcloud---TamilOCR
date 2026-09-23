/* Pink Cloud - landing intro (index.html).
   Plays the chosen intro once per browser session, over the real upload
   page, then slides the sheet up and springs the page in underneath.

   No glow or coloured shadow behind the marks: just dots on the canvas
   colour.

   Variants (all share one timeline, springs and colours from tokens.css;
   ported from design-drafts/pinkcloud-intro.html, fake product page removed,
   the real page animates in instead):
     logo       the Pink Cloud logo (logo2.svg): cloud mark materialises,
                springs up, "Pinkcloud" wordmark builds in beneath (default)
     pinkcloud  bolt + "Pink Cloud" in dot type
     omni       bolt + "Omni", exactly as the draft spelled it

   Controls:
     ?intro=1          force it to play (ignores the once-per-session flag)
     ?intro=0          never play
     ?intro=<variant>  play logo / pinkcloud / omni and remember that choice
                       in this browser (localStorage pc.intro.variant)
     any ?intro=...    shows a small picker on the sheet to flip variants
     ?t=2.5            freeze at a time (debug, same as the draft)
   Skipped automatically for prefers-reduced-motion and the scripted
   upload demo (?mock=1&demo). Click, Esc, Enter, Space or "Skip" jumps to
   the slide-up.
   Load: intro.css in <head>, this script as the FIRST element in <body>
   (before the page markup) so the page never flashes before the sheet. */
(function () {
  "use strict";

  var QS = new URLSearchParams(location.search);
  var SEEN_KEY = "pc.intro.seen";
  var VARIANT_KEY = "pc.intro.variant";
  var DEFAULT_VARIANT = "logo";
  var param = QS.get("intro");

  function seen() { try { return sessionStorage.getItem(SEEN_KEY) === "1"; } catch (e) { return false; } }
  function markSeen() { try { sessionStorage.setItem(SEEN_KEY, "1"); } catch (e) { /* private mode */ } }

  var forced = param !== null && param !== "0";
  if (param === "0") return;
  if (!forced && seen()) return;
  if (!forced && QS.has("demo")) return;
  if (!forced && window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) { markSeen(); return; }

  /* ================= easing (from the draft, exact) ================= */
  function bezier(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx, cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    function sx(t) { return ((ax * t + bx) * t + cx) * t; }
    function sy(t) { return ((ay * t + by) * t + cy) * t; }
    function dx(t) { return (3 * ax * t + 2 * bx) * t + cx; }
    return function (x) {
      if (x <= 0) return 0; if (x >= 1) return 1;
      var t = x, i;
      for (i = 0; i < 8; i++) { var e = sx(t) - x, d = dx(t); if (Math.abs(e) < 1e-6 || Math.abs(d) < 1e-6) break; t -= e / d; }
      var lo = 0, hi = 1;
      for (i = 0; i < 20 && Math.abs(sx(t) - x) > 1e-5; i++) { if (sx(t) < x) lo = t; else hi = t; t = (lo + hi) / 2; }
      return sy(t);
    };
  }
  function spring(k, c) {
    var w0 = Math.sqrt(k), z = c / (2 * Math.sqrt(k)), wd = w0 * Math.sqrt(Math.max(1e-6, 1 - z * z));
    return function (t) { if (t <= 0) return 0; return 1 - Math.exp(-z * w0 * t) * (Math.cos(wd * t) + (z * w0 / wd) * Math.sin(wd * t)); };
  }
  var E = {
    dotPop: spring(260, 13),
    boltShift: spring(170, 15),
    productIn: spring(210, 17),
    glowFlare: bezier(.16, 1, .3, 1),
    sheetUp: bezier(.8, -.35, .2, 1),
    fade: bezier(.33, 1, .68, 1)
  };
  function clamp(v, a, b) { a = a === undefined ? 0 : a; b = b === undefined ? 1 : b; return Math.min(b, Math.max(a, v)); }
  function seg(t, a, b) { return clamp((t - a) / (b - a)); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function hash(i) { var x = Math.sin(i * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); }

  /* ================= palette from tokens.css ================= */
  var css = getComputedStyle(document.documentElement);
  function tok(name, fallback) { var v = css.getPropertyValue(name).trim(); return v || fallback; }
  function rgb(hex) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h.split("").map(function (c) { return c + c; }).join("");
    return parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16);
  }
  var C = {
    bg: tok("--pc-color-bg-canvas", "#FDFCFB"),
    ink: tok("--pc-color-text-primary", "#1E1A18"),
    dark: tok("--pc-color-primary-active", "#B32E08"),
    orange: tok("--pc-color-primary", "#F94612"),
    light: tok("--pc-color-primary-gradient-end", "#FA683D"),
    white: tok("--pc-color-on-primary", "#FFFFFF")
  };
  C.orangeRgb = rgb(C.orange);
  C.lightRgb = rgb(C.light);

  function dotter(ctx) { return function (x, y, r, fill) { if (r <= .15) return; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fillStyle = fill; ctx.fill(); }; }
  /* dot grid sampler shared by the variants: draws a shape offscreen and
     keeps one dot per grid cell with enough coverage */
  function sample(drawFn, w, h, step) {
    var o = document.createElement("canvas"); o.width = w; o.height = h;
    var g = o.getContext("2d"); g.fillStyle = "#000"; drawFn(g, w, h);
    var d = g.getImageData(0, 0, w, h).data, out = [];
    var offs = [[0, 0], [-step * .3, 0], [step * .3, 0], [0, -step * .3], [0, step * .3]];
    for (var y = step / 2; y < h; y += step) for (var x = step / 2; x < w; x += step) {
      var cov = 0, n = 0;
      for (var k = 0; k < offs.length; k++) {
        var px = Math.round(x + offs[k][0]), py = Math.round(y + offs[k][1]);
        if (px < 0 || py < 0 || px >= w || py >= h) continue;
        cov += d[(py * w + px) * 4 + 3] / 255; n++;
      }
      cov /= n; if (cov > .2) out.push({ x: x, y: y, cov: cov });
    }
    return out;
  }

  /* ================= variant: bolt + word (design-drafts/pinkcloud-intro.html) ================= */
  function boltIntro(ctx, WORD) {
    var W, H, pitch, textDots = [], boltDots = [], L = {};
    var FONT_STACK = 'ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif';

    function build(w, h) {
      W = w; H = h;
      /* "Pink Cloud" is much wider than the draft's "Omni": size the type so
         bolt + gap + word fill at most 88% of the width, and keep the draft's
         dot pitch to type size ratio (unit/46 : unit*0.42 = 1 : 19.3). */
      var unit = Math.min(W / 1.9, H * 0.9);
      var m = document.createElement("canvas").getContext("2d");
      var fontPx = Math.round(unit * 0.42);
      m.font = "900 " + fontPx + "px " + FONT_STACK;
      var wordW = m.measureText(WORD).width;
      var totalW = wordW + fontPx * 0.70 + fontPx * 0.26; /* word + bolt + gap/padding */
      fontPx = Math.round(fontPx * Math.min(1, (W * 0.88) / totalW));
      pitch = Math.max(4, Math.round(fontPx / 19.3));
      var font = "900 " + fontPx + "px " + FONT_STACK;
      m.font = font;
      var tw = Math.ceil(m.measureText(WORD).width) + pitch * 2, th = Math.ceil(fontPx * 1.05);
      var td = sample(function (g, w2, h2) { g.font = font; g.textBaseline = "middle"; g.fillText(WORD, pitch, h2 * 0.52); }, tw, th, pitch);
      var bh = th * 1.08, bw = bh * 0.62;
      var bd = sample(function (g, w2, h2) {
        g.beginPath();
        var P = [[.62, 0], [.05, .58], [.44, .58], [.30, 1], [.95, .38], [.55, .38], [.78, 0]];
        P.forEach(function (p, i) { if (i) g.lineTo(p[0] * w2, p[1] * h2); else g.moveTo(p[0] * w2, p[1] * h2); });
        g.closePath(); g.fill();
      }, Math.ceil(bw), Math.ceil(bh), pitch);
      var gap = pitch * 3, total = bw + gap + tw;
      L = { cx: W / 2, cy: H / 2, total: total, bw: bw, bh: bh, tw: tw, th: th, gap: gap,
        boltEndX: W / 2 - total / 2, boltStartX: W / 2 - bw / 2, textX: W / 2 - total / 2 + bw + gap };
      boltDots = bd.map(function (d, i) { var dx = d.x - bw / 2, dy = d.y - bh / 2; return { x: d.x, y: d.y, cov: d.cov, dx: dx, dy: dy, dist: Math.hypot(dx, dy) / bh, h: hash(i + 7) }; });
      textDots = td.map(function (d, i) { return { x: d.x, y: d.y, cov: d.cov, u: d.x / tw, v: d.y / th, h: hash(i + 1001) }; });
    }

    var dot = dotter(ctx);

    function frame(t) {
      ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      var R = pitch * 0.5;
      var shift = E.boltShift(Math.max(0, t - 1.10));
      var bx = lerp(L.boltStartX, L.boltEndX, shift), by = L.cy - L.bh / 2, bcx = bx + L.bw / 2;

      var i, d;
      for (i = 0; i < boltDots.length; i++) {
        d = boltDots[i];
        var s = E.dotPop(t - (.25 + d.dist * .55 + d.h * .12));
        var wave = .5 + .5 * Math.sin(t * 4.2 - d.dy / pitch * .55 + d.h * 1.5);
        var r = R * (.45 + .55 * d.cov) * s * (.72 + .42 * wave * seg(t, 1.2, 1.6));
        var x = bx + d.x, y = by + d.y;
        dot(x, y, r, wave > .62 ? C.orange : (wave > .3 ? C.light : C.dark));
        if (wave > .8) dot(x, y, r * .38, C.white);
      }
      for (i = 0; i < textDots.length; i++) {
        d = textDots[i];
        var s2 = E.dotPop(t - (1.20 + (d.u * .85 + d.v * .25) * 1.0 + d.h * .10));
        var live = seg(t, 2.2, 2.7);
        var w1 = .5 + .5 * Math.sin(t * 3.4 - (d.u * 7 + d.v * 3) + d.h * .8);
        var w2 = .5 + .5 * Math.sin(t * 1.7 + d.u * 11 - d.v * 2);
        var wv = lerp(.35, w1 * .7 + w2 * .3, live);
        var pulse = 1 + live * (wv - .45) * .75;
        var r2 = R * (.42 + .58 * d.cov) * s2 * pulse;
        var x2 = L.textX + d.x, y2 = L.cy - L.th / 2 + d.y;
        var mix = lerp(d.v + d.h * .15, wv, live);
        dot(x2, y2, r2, mix > .68 ? C.orange : (mix > .42 ? C.dark : C.ink));
        if (live > 0 && wv > .86 && d.cov > .8) dot(x2, y2, r2 * .36, C.white);
      }
    }

    /* EXIT: sheet starts leaving; SHEET_END: fully off; PRODUCT_AT: page spring starts; END: done */
    return timed({ build: build, frame: frame });
  }


  /* shared timing: every variant leaves on the same beat */
  function timed(v) { v.EXIT = 3.70; v.SHEET_END = 4.60; v.PRODUCT_AT = 3.95; v.FADE_AT = [3.85, 4.4]; v.END = 4.9; return v; }

  /* ================= variant: logo (logo2.svg) =================
     Same timeline as the bolt variant: the cloud mark materialises from its
     centre, then springs up into place while the
     "Pinkcloud" wordmark builds in left to right beneath it. Shapes are the
     paths of logo2.svg (viewBox 0 0 80 80), inlined so the first frame
     never waits on a network fetch. */
  var LOGO_MARK = [
      "M33.44 43.58L27.66 43.58C24.65 43.58 22.38 41.16 22.38 38.39C22.43 34.78 25.56 32.26 29.02 32.26C32.65 32.26 35.57 35.1 35.57 38.52L35.57 40.07C35.57 45.48 40.36 49.08 45.67 49.08L52.1 49.08C58.93 49.08 63.83 43.97 63.83 38.11C63.83 31.91 59.17 27.31 53.74 26.72C53.9 28.81 53.74 31.31 53.11 32.62C55.58 33.38 57.37 35.49 57.37 38.11C57.37 41.22 54.67 43.57 51.58 43.57L46.59 43.57C43.7 43.57 41.47 41.52 41.47 38.71L41.47 37.53C41.47 30.82 35.6 26.11 29.02 26.11C21.66 26.22 16.08 31.7 16.08 38.16C16.08 44.58 21.24 49.08 27.37 49.08L33.44 49.08C33.47 49.08 33.47 49.03 33.45 49.06C33.5 47.32 33.47 45.06 33.44 43.58Z",
      "m39.67 15.79c-5.52 0-10.49 4.1-11.64 8.3 2.28-0.15 4.8 0.24 6.5 1.05 1.1-1.81 3.01-2.97 5.14-2.97 3.32 0 6 2.7 6 5.98v0.94c0 2.88-0.92 5.24-2.49 6.3-0.01 0.59 0.04 1.16 0.06 1.63 3.99-0.03 8.68-2.76 8.68-8.78v-0.22c0-6.85-5.44-12.23-12.25-12.23z"
    ];
  var LOGO_WORD = [
      "M15.323999999999998 52.89a1.556 1.556 0 1 0 3.112 0a1.556 1.556 0 1 0 -3.112 0z",
      "m10.09 52.36h-6.07v11.56h2.43v-2.94h3.11c2.9 0 5.08-1.7 5.08-4.42 0-2.49-1.87-4.2-4.55-4.2zm-0.42 6.15h-3.17v-3.82h3.17c1.36 0 2.16 0.82 2.16 1.89 0 1.18-0.91 1.93-2.16 1.93z",
      "M15.63 55.32h2.517v8.599h-2.517z",
      "m23.47 55.05c-2.35 0-4.21 1.79-4.21 4.33v4.54h2.57v-4.61c0-1.17 0.76-1.75 1.68-1.75 0.93 0 1.68 0.62 1.68 1.72v4.64h2.57v-4.53c0-2.63-1.85-4.34-4.29-4.34z",
      "m37.26 55.32h-2.95l-3.08 3.27v-7.06h-2.58v12.39h2.57v-1.79l1.11-1.09 1.99 2.88h2.91l-3.24-4.86 3.27-3.74z",
      "m41.65 61.84c-1.32 0-2.22-1.02-2.22-2.2 0-1.3 0.99-2.15 2.22-2.15 0.88 0 1.58 0.45 1.91 0.95l1.74-1.66c-0.81-1.08-2.18-1.73-3.67-1.73-2.63 0-4.7 1.97-4.7 4.61 0 2.72 2.29 4.59 4.7 4.59 1.6 0 3.13-0.85 3.7-1.76l-1.79-1.51c-0.37 0.51-1.14 0.86-1.89 0.86z",
      "M46.02 52.38h2.541v11.54h-2.541z",
      "m53.85 55.05c-2.6 0-4.6 2.01-4.6 4.58 0 2.63 2.22 4.61 4.6 4.61 2.46-0.02 4.68-2.03 4.68-4.61 0-2.6-2.08-4.58-4.68-4.58zm0.03 6.72c-1.27 0-2.17-0.99-2.17-2.14 0-1.28 0.99-2.12 2.15-2.12 1.25 0 2.19 0.91 2.19 2.12 0 1.24-1.03 2.14-2.17 2.14z",
      "m64.61 60.19c0 1.04-0.73 1.52-1.47 1.52-0.81 0-1.51-0.55-1.51-1.54v-4.85h-2.46v4.9c0 2.4 1.88 3.95 3.95 3.95 2.1 0 3.92-1.71 3.92-3.93v-4.92h-2.43v4.87z",
      "m73.51 51.53v4.12c-0.56-0.32-1.2-0.49-1.82-0.49-2.3 0.03-4.18 1.93-4.18 4.48 0 2.69 2 4.5 4.22 4.5 2.36-0.01 4.34-1.87 4.34-4.43v-8.18h-2.56zm-1.75 10.23c-1.21-0.02-1.99-0.97-1.99-2.1 0.02-1.32 0.99-2.1 2.01-2.1 1.16 0.01 1.99 0.86 1.99 2.07 0 1.33-0.99 2.13-2.01 2.13z"
    ];
  var LOGO_BOX = { x: 4.0, y: 15.8, w: 72.0, h: 48.4, markX: 16.0, markY: 15.8, markW: 48.0, markH: 33.4, wordY: 51.5, wordH: 12.8 };

  function logoIntro(ctx) {
    var W, H, pitch, s, markDots = [], wordDots = [], L = {};
    function drawPaths(list, ox, oy) {
      return function (g) {
        g.save(); g.scale(s, s); g.translate(-ox, -oy);
        list.forEach(function (d) { g.fill(new Path2D(d)); });
        g.restore();
      };
    }
    function build(w, h) {
      W = w; H = h;
      var B = LOGO_BOX;
      s = Math.min((H * 0.60) / B.h, (W * 0.80) / B.w);
      pitch = Math.max(3, Math.round(s * 0.62));
      var mw = Math.ceil(B.markW * s), mh = Math.ceil(B.markH * s);
      var ww = Math.ceil(B.w * s), wh = Math.ceil(B.wordH * s);
      var md = sample(drawPaths(LOGO_MARK, B.markX, B.markY), mw, mh, pitch);
      var wd = sample(drawPaths(LOGO_WORD, B.x, B.wordY), ww, wh, pitch);
      var total = B.h * s;
      var top = H / 2 - total / 2;
      L = { cx: W / 2, mw: mw, mh: mh, ww: ww, wh: wh,
        markX: W / 2 - (B.w / 2 - (B.markX - B.x)) * s,
        markStartY: H / 2 - mh / 2, markEndY: top,
        wordX: W / 2 - ww / 2, wordY: top + (B.wordY - B.y) * s };
      markDots = md.map(function (d, i) { var dx = d.x - mw / 2, dy = d.y - mh / 2; return { x: d.x, y: d.y, cov: d.cov, dy: dy, dist: Math.hypot(dx, dy) / mh, h: hash(i + 7) }; });
      wordDots = wd.map(function (d, i) { return { x: d.x, y: d.y, cov: d.cov, u: d.x / ww, v: d.y / wh, h: hash(i + 1001) }; });
    }
    var dot = dotter(ctx);
    function frame(t) {
      ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      var R = pitch * 0.5;
      var shift = E.boltShift(Math.max(0, t - 1.10));
      var my = lerp(L.markStartY, L.markEndY, shift), mcx = L.markX + L.mw / 2, mcy = my + L.mh / 2;

      var i, d;
      for (i = 0; i < markDots.length; i++) {
        d = markDots[i];
        var sc = E.dotPop(t - (.25 + d.dist * .55 + d.h * .12));
        var wave = .5 + .5 * Math.sin(t * 4.2 - d.dy / pitch * .55 + d.h * 1.5);
        var r = R * (.45 + .55 * d.cov) * sc * (.72 + .42 * wave * seg(t, 1.2, 1.6));
        var x = L.markX + d.x, y = my + d.y;
        dot(x, y, r, wave > .62 ? C.orange : (wave > .3 ? C.light : C.dark));
        if (wave > .8) dot(x, y, r * .38, C.white);
      }
      for (i = 0; i < wordDots.length; i++) {
        d = wordDots[i];
        var s2 = E.dotPop(t - (1.20 + (d.u * .85 + d.v * .25) * 1.0 + d.h * .10));
        var live = seg(t, 2.2, 2.7);
        var w1 = .5 + .5 * Math.sin(t * 3.4 - (d.u * 7 + d.v * 3) + d.h * .8);
        var w2 = .5 + .5 * Math.sin(t * 1.7 + d.u * 11 - d.v * 2);
        var wv = lerp(.35, w1 * .7 + w2 * .3, live);
        var pulse = 1 + live * (wv - .45) * .75;
        var r2 = R * (.42 + .58 * d.cov) * s2 * pulse;
        var x2 = L.wordX + d.x, y2 = L.wordY + d.y;
        var mix = lerp(d.v + d.h * .15, wv, live);
        dot(x2, y2, r2, mix > .68 ? C.orange : (mix > .42 ? C.dark : C.ink));
        if (live > 0 && wv > .86 && d.cov > .8) dot(x2, y2, r2 * .36, C.white);
      }
    }
    return timed({ build: build, frame: frame });
  }

  /* variant picker: ?intro=<name> plays that one and remembers it for this
     browser; with any ?intro= in the URL a small picker shows on the sheet */
  var VARIANTS = {
    logo: { label: "Logo", make: function (ctx) { return logoIntro(ctx); } },
    pinkcloud: { label: "Pink Cloud", make: function (ctx) { return boltIntro(ctx, "Pink Cloud"); } },
    omni: { label: "Omni", make: function (ctx) { return boltIntro(ctx, "Omni"); } }
  };
  var stored = null;
  try { stored = localStorage.getItem(VARIANT_KEY); } catch (e) { /* private mode */ }
  var variantName = (param && VARIANTS[param]) ? param : ((stored && VARIANTS[stored]) ? stored : DEFAULT_VARIANT);
  if (param && VARIANTS[param]) { try { localStorage.setItem(VARIANT_KEY, param); } catch (e) { /* private mode */ } }

  /* ================= overlay ================= */
  var root = document.createElement("div");
  root.className = "pc-intro";
  root.setAttribute("aria-hidden", "true");
  var cv = document.createElement("canvas");
  root.appendChild(cv);
  var skip = document.createElement("button");
  skip.type = "button";
  skip.className = "btn btn--soft btn--sm pc-intro-skip";
  skip.setAttribute("aria-label", "Skip intro");
  skip.innerHTML = 'Skip<span class="kbd" aria-hidden="true">Esc</span>';
  root.appendChild(skip);
  if (param !== null) {
    var pick = document.createElement("div");
    pick.className = "pc-intro-pick";
    pick.setAttribute("role", "group");
    pick.setAttribute("aria-label", "Intro style");
    Object.keys(VARIANTS).forEach(function (k) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn--sm " + (k === variantName ? "btn--primary" : "btn--soft");
      b.textContent = VARIANTS[k].label;
      b.setAttribute("aria-pressed", k === variantName ? "true" : "false");
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var q = new URLSearchParams(location.search); q.set("intro", k); q.delete("t");
        location.search = q.toString();
      });
      pick.appendChild(b);
    });
    root.appendChild(pick);
  }
  document.documentElement.classList.add("pc-intro-on");
  (document.body || document.documentElement).insertBefore(root, (document.body || document.documentElement).firstChild);

  var ctx = cv.getContext("2d");
  var V = VARIANTS[variantName].make(ctx);
  function size() {
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = innerWidth * dpr; cv.height = innerHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    V.build(innerWidth, innerHeight);
  }
  size();

  /* the real page (everything in <body> except the sheet) springs in */
  function pageEls() {
    var out = [], kids = document.body ? document.body.children : [];
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (k === root || k.tagName === "SCRIPT" || k.hidden) continue;
      out.push(k);
    }
    return out;
  }
  function layers(t) {
    var up = E.sheetUp(seg(t, V.EXIT, V.SHEET_END));
    root.style.transform = "translate3d(0," + (-up * 104) + "%,0)";
    var p = E.productIn(Math.max(0, t - V.PRODUCT_AT));
    var op = clamp(E.fade(seg(t, V.FADE_AT[0], V.FADE_AT[1])));
    pageEls().forEach(function (el) {
      if (el.id === "demo-modal") return;
      el.style.transform = "translate3d(0," + ((1 - p) * 70) + "px,0) scale(" + (.965 + .035 * p) + ")";
      el.style.opacity = String(op);
    });
  }

  var done = false;
  function finish() {
    if (done) return;
    done = true;
    markSeen();
    pageEls().forEach(function (el) { el.style.transform = ""; el.style.opacity = ""; });
    root.remove();
    document.documentElement.classList.remove("pc-intro-on");
    removeEventListener("keydown", onKey, true);
    removeEventListener("resize", size);
  }

  var frozen = QS.has("t") ? parseFloat(QS.get("t")) : null;
  addEventListener("resize", size);
  function render(t) { if (t < V.SHEET_END + .1) V.frame(t); layers(t); }

  if (frozen !== null && !isNaN(frozen)) {
    /* debug still: render once after the page markup exists */
    document.addEventListener("DOMContentLoaded", function () { render(frozen); window.__introReady = true; });
    return;
  }

  var start = null;
  function tick(now) {
    if (done) return;
    if (start === null) start = now;
    var t = (now - start) / 1000;
    render(t);
    if (t >= V.END) { finish(); return; }
    requestAnimationFrame(tick);
  }
  /* skip = jump to the slide-up, never a hard cut */
  function skipNow() {
    if (start === null || done) return;
    var t = (performance.now() - start) / 1000;
    if (t < V.EXIT) start = performance.now() - V.EXIT * 1000;
  }
  function onKey(e) {
    if (e.key === "Escape" || e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); skipNow(); }
  }
  root.addEventListener("click", skipNow);
  addEventListener("keydown", onKey, true);
  requestAnimationFrame(tick);
  markSeen(); /* a reload mid-intro does not replay it */
})();
