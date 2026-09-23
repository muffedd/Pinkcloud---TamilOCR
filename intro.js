/* Pink Cloud - landing intro (index.html).
   Plays the chosen intro once per browser session, over the real upload
   page, then slides the sheet up and springs the page in underneath.

   Chosen variant: "omni" = design-drafts/pinkcloud-intro.html (the first
   intro, bubble-fixed). The drawing code is ported from that draft as-is
   (timeline, springs, bezier curves), except the word reads "Pink Cloud"
   instead of the draft's "Omni"; the fake product page is gone,
   the real page is animated instead, and colours come from tokens.css.

   Swapping: add a variant to VARIANTS (same shape as omniIntro: build,
   frame, EXIT, END) and change DEFAULT_VARIANT.

   Controls:
     ?intro=1          force it to play (ignores the once-per-session flag)
     ?intro=0          never play
     ?intro=<variant>  force a specific variant
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
  var DEFAULT_VARIANT = "omni";
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

  /* ================= variant: omni (design-drafts/pinkcloud-intro.html) ================= */
  function omniIntro(ctx) {
    var W, H, pitch, textDots = [], boltDots = [], L = {};
    var WORD = "Pink Cloud"; /* the draft spelled "Omni" (reference leftover) */
    var FONT_STACK = 'ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif';

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

    function dot(x, y, r, fill) { if (r <= .15) return; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fillStyle = fill; ctx.fill(); }
    function glow(x, y, rad, a, col) {
      var g = ctx.createRadialGradient(x, y, 0, x, y, rad);
      g.addColorStop(0, "rgba(" + col + "," + a + ")"); g.addColorStop(.45, "rgba(" + col + "," + (a * .45) + ")"); g.addColorStop(1, "rgba(" + col + ",0)");
      ctx.fillStyle = g; ctx.fillRect(x - rad, y - rad, rad * 2, rad * 2);
    }

    function frame(t) {
      ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      var R = pitch * 0.5;
      var shift = E.boltShift(Math.max(0, t - 1.10));
      var bx = lerp(L.boltStartX, L.boltEndX, shift), by = L.cy - L.bh / 2, bcx = bx + L.bw / 2;

      var gIn = E.fade(seg(t, 0, .9));
      var flare = E.glowFlare(seg(t, .95, 1.35)) * (1 - E.fade(seg(t, 1.35, 2.4)));
      var breathe = .5 + .5 * Math.sin(t * 2.2);
      glow(bcx, L.cy, L.bh * (1.05 + .5 * flare), .30 * gIn + .35 * flare, C.orangeRgb);
      glow(bcx, L.cy, L.bh * (.55 + .25 * flare), .22 * gIn + .30 * flare, C.lightRgb);
      var tIn = E.fade(seg(t, 1.3, 2.4));
      glow(L.textX + L.tw * .5, L.cy, L.tw * .62, (.16 + .05 * breathe) * tIn, C.lightRgb);
      glow(L.textX + L.tw * .25, L.cy + L.th * .1, L.tw * .42, (.10 + .04 * breathe) * tIn, C.orangeRgb);

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
    return { build: build, frame: frame, EXIT: 3.70, SHEET_END: 4.60, PRODUCT_AT: 3.95, FADE_AT: [3.85, 4.4], END: 4.9 };
  }

  var VARIANTS = { omni: omniIntro };
  var variantName = (param && VARIANTS[param]) ? param : DEFAULT_VARIANT;

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
  document.documentElement.classList.add("pc-intro-on");
  (document.body || document.documentElement).insertBefore(root, (document.body || document.documentElement).firstChild);

  var ctx = cv.getContext("2d");
  var V = VARIANTS[variantName](ctx);
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
