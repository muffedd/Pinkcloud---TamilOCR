/* Pink Cloud correction editor (page 2 of 3).
   Vanilla JS, fully offline. One selection model drives the scan,
   the text pane and the review queue.
   Accepted fixes are saved back to the backend (GET+PUT /jobs/{id}/corrections,
   schema/corrections-endpoint.md; localStorage fallback only when the backend
   is unreachable or predates the route) and feed a client-side corrections
   dictionary that auto-applies to future jobs. */

(function () {
"use strict";

/* Correction tier -> provenance. (Tier map to confirm with the team.) */
var TIER_PROV = { T1: "swap", T2: "rule", T3: "llm" };

/* Backend serves the page image at 1600px wide; bboxes use that space. */
var IMG_W = 1600;
/* Mock coordinate-space height for the placeholder paper. */
var PAGE_H = 1400;
/* Boxes are padded 3px outside their bbox. */
var BOX_PAD = 3;

var ICONS = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg>',
  wrench: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
  checkCircle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16.5 9"/></svg>'
};

var S = {
  page: null,
  lines: [],
  words: [],
  byKey: {},
  queue: [],
  activeKey: null,
  hoverKey: null,
  hoverLine: null,
  mode: "auto",
  lens: "conf",
  heat: false,
  popupKey: null,
  scale: 1,
  corrections: {},       /* word key -> {page, line, word, before, after} */
  saveMode: "unknown",   /* "unknown" | "server" | "local" */
  saveError: false,      /* last server save/load failed with a real error (500 etc.) */
  corrDirty: false,      /* correction map changed since the last successful sync */
  dictCount: 0           /* fixes auto-applied to this page from the dictionary */
};

var AUTO_MIN = 0.9;
var OK_MIN = 0.75;
var MOTION_BASE = 160;
var TOAST_MS = 2400;

/* Corrections save-back (contract: schema/corrections-endpoint.md; route
   live via PC_API.getCorrections / PC_API.saveCorrections). PUT replaces the
   job's full correction map. localStorage keyed by job id is the fallback
   cache ONLY for network failure or a 404 (older backend without the route);
   a server error (500) is surfaced to the reviewer, never silently absorbed. */
var CORR_SAVE_MS = 800;
var LS_CORR = "pc.corrections.";  /* + job id: saved correction list */
var LS_DICT = "pc.fixdict";       /* global across jobs: OCR word -> accepted correction */
var LS_SKIPS = "pc.dictskips.";   /* + job id: auto-applied fixes the reviewer undid */
/* localStorage id for the offline demo (no real job id in mock mode). */
var MOCK_STORAGE_ID = "demo-kural";

var el = {};
var saveTimer = null;
var saveWarned = false;

function $(id) { return document.getElementById(id); }

function cssNum(name, fallback) {
  var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
  return isNaN(v) ? fallback : v;
}

/* ---------------- model ---------------- */

function binOf(conf) {
  if (conf >= AUTO_MIN) return "auto";
  if (conf >= OK_MIN) return "ok";
  return "doubt";
}

function buildModel(doc) {
  S.page = doc;
  S.lines = [];
  S.words = [];
  S.byKey = {};
  var corrections = doc.corrections || [];
  var ordered = doc.lines.slice().sort(function (a, b) { return a.seq - b.seq; });
  ordered.forEach(function (line) {
    var entry = { id: line.id, seq: line.seq, body: line.body, bbox: line.bbox, conf: line.confidence, words: [] };
    var parts = String(line.body).split(/\s+/).filter(Boolean);
    var total = parts.reduce(function (s, w) { return s + w.length; }, 0) || 1;
    var x = line.bbox[0];
    parts.forEach(function (text, i) {
      /* No words[] in the contract: split the line bbox in proportion
         to character count. These boxes are approximate. */
      var w = line.bbox[2] * (text.length / total);
      var bbox = [Math.round(x), line.bbox[1], Math.round(w), line.bbox[3]];
      x += w;
      var hit = null;
      for (var c = 0; c < corrections.length; c++) {
        if (corrections[c].before === text) { hit = corrections[c]; break; }
      }
      var word = {
        key: "p" + doc.page + ":" + line.id + ":w" + (i + 1),
        page: doc.page,
        lineId: line.id,
        seq: line.seq,
        idx: i + 1,
        text: text,
        orig: text,
        after: hit ? hit.after : "",
        conf: line.confidence,
        bin: binOf(line.confidence),
        prov: hit ? (TIER_PROV[hit.tier] || "raw") : "raw",
        tier: hit ? hit.tier : "",
        evidence: hit ? hit.evidence : "",
        target: !!hit,
        bbox: bbox,
        approx: true,
        fixed: false,
        autoApplied: false,  /* text came from the corrections dictionary, not the reviewer */
        autoPrev: null,      /* pre-auto-apply suggestion state, for undo */
        line: entry,
        el: null,
        boxEl: null,
        rowEl: null
      };
      entry.words.push(word);
      S.words.push(word);
      S.byKey[word.key] = word;
    });
    S.lines.push(entry);
  });
  rebuildQueue();
}

function rebuildQueue() {
  S.queue = S.words.filter(function (w) {
    if (S.mode === "auto") return w.bin === "doubt";
    return w.bin === "doubt" || w.bin === "ok";
  });
}

function fixedCount() {
  return S.queue.filter(function (w) { return w.fixed; }).length;
}

function pageState() {
  var doc = S.page;
  if (!doc) return "queued";
  if (doc.profile === "HEAVY" && doc.preprocessed) return "repaired";
  if (doc.profile === "FAST" && !doc.needs_review) return "clean";
  if (doc.preprocessed) return "precomputed";
  return "queued";
}

/* ---------------- corrections: save-back + fix-list dictionary ---------------- */

function lsGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function lsSet(key, val) {
  try { window.localStorage.setItem(key, val); } catch (e) { /* private mode: stay session-only */ }
}

/* localStorage key owner: the real job id, or the demo id in mock mode. */
function storageId() {
  return S.jobId || (window.PC_API.USE_MOCK ? MOCK_STORAGE_ID : null);
}

/* Full-map payload, sorted into reading order (page, line, word). */
function corrPayload() {
  var list = Object.keys(S.corrections).map(function (k) { return S.corrections[k]; });
  list.sort(function (a, b) {
    return a.page - b.page || String(a.line).localeCompare(String(b.line)) || a.word - b.word;
  });
  return { corrections: list };
}

function savedCorrectionsLocal() {
  var id = storageId();
  if (!id) return [];
  try {
    var v = JSON.parse(lsGet(LS_CORR + id) || "[]");
    return Array.isArray(v) ? v : [];
  } catch (e) { return []; }
}

function saveLocal() {
  var id = storageId();
  if (!id) return;
  lsSet(LS_CORR + id, JSON.stringify(corrPayload().corrections));
}

/* Debounced save-back: the local copy is written on every change (crash-safe
   and the export fallback source); the server PUT follows after a quiet
   moment, unless the route already proved missing this session. */
function scheduleSave() {
  saveLocal();
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSave, CORR_SAVE_MS);
}

/* Awaitable: callers (Export) can wait for the pending write. Flushes NOW -
   clears a pending debounce timer. Sends whenever the map changed since the
   last successful sync, INCLUDING an empty map: a reviewer who cleared every
   fix must clear the server's copy too (PUT [] empties it), otherwise export
   would keep applying stale fixes. */
function flushSave() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  if (window.PC_API.USE_MOCK || !S.jobId) return Promise.resolve(); /* the demo stays local-only */
  if (!S.corrDirty) return Promise.resolve(); /* nothing changed since the last sync */
  if (S.saveMode === "local") { /* route known missing - local copy is current */
    S.corrDirty = false;
    return Promise.resolve();
  }
  return window.PC_API.saveCorrections(S.jobId, corrPayload().corrections).then(function () {
    S.saveMode = "server";
    S.saveError = false;
    S.corrDirty = false;
  }).catch(function (err) {
    if (err && err.status === 404) {
      /* Older backend without the route (the job itself loaded fine): switch
         to the silent local fallback for the rest of the session. No error. */
      S.saveMode = "local";
      return;
    }
    if (err && err.kind === "down") {
      /* Network failure: the local copy is already written; say so once. */
      if (!saveWarned) {
        saveWarned = true;
        toast("Corrections saved locally · backend endpoint unreachable");
      }
      return;
    }
    /* Real server error (500 "corrections unreadable", 422 bad payload, ...):
       NOT a fallback case - surface it. The localStorage cache still has the
       fixes for this session, but they are NOT on the server. */
    S.saveError = true;
    toast("Server could not save corrections (" + ((err && err.status) || "error") +
      ") · fixes kept locally for this session only");
  });
}

function recordCorrection(w) {
  S.corrections[w.key] = { page: w.page, line: w.lineId, word: w.idx, before: w.orig, after: w.text };
  S.corrDirty = true;
  scheduleSave();
}

/* Rejoin line bodies + page text after any word text change. */
function retext() {
  S.lines.forEach(function (l) { l.body = l.words.map(function (x) { return x.text; }).join(" "); });
  S.page.text = S.lines.map(function (l) { return l.body; }).join("\n");
}

/* Reapply corrections from a previous session (server list or local fallback). */
function applySavedCorrections(list) {
  if (!list || !list.length) return;
  var touched = false;
  list.forEach(function (c) {
    var w = S.byKey["p" + c.page + ":" + c.line + ":w" + c.word];
    if (!w) return;
    w.text = c.after;
    w.prov = "human";
    w.target = false;
    w.fixed = true;
    S.corrections[w.key] = { page: c.page, line: c.line, word: c.word, before: c.before, after: c.after };
    touched = true;
  });
  if (touched) retext();
}

/* The learning layer: every accepted fix teaches one OCR word -> correction
   pair, reused across jobs. */
function dictLoad() {
  try {
    var v = JSON.parse(lsGet(LS_DICT) || "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  } catch (e) { return {}; }
}

function dictAdd(orig, after) {
  if (!orig || !after || orig === after) return;
  var d = dictLoad();
  d[orig] = after;
  lsSet(LS_DICT, JSON.stringify(d));
}

function skipsLoad() {
  var id = storageId();
  if (!id) return [];
  try {
    var v = JSON.parse(lsGet(LS_SKIPS + id) || "[]");
    return Array.isArray(v) ? v : [];
  } catch (e) { return []; }
}

function skipAdd(key) {
  var id = storageId();
  if (!id) return;
  var skips = skipsLoad();
  if (skips.indexOf(key) === -1) {
    skips.push(key);
    lsSet(LS_SKIPS + id, JSON.stringify(skips));
  }
}

/* Auto-apply dictionary matches to this page's reviewable words. High-
   confidence (auto-bin) words stay untouched, and a word the reviewer
   previously undid is not re-applied. */
function applyDictionary() {
  S.dictCount = 0;
  var dict = dictLoad();
  var skips = skipsLoad();
  S.words.forEach(function (w) {
    var after = dict[w.orig];
    if (!after || after === w.orig) return;
    if (w.fixed) return;      /* saved human corrections win over the dictionary */
    if (w.bin === "auto") return;
    if (skips.indexOf(w.key) !== -1) return;
    w.autoPrev = { after: w.after, target: w.target, prov: w.prov, tier: w.tier, evidence: w.evidence };
    w.text = after;
    w.after = "";
    w.target = false;
    w.fixed = true;
    w.autoApplied = true;
    S.dictCount++;
  });
  if (S.dictCount) retext();
}

/* Load this job's corrections, then the dictionary, then render. Live mode
   asks the backend first; a 404 means the route is not deployed (the job
   loaded fine), so the local fallback takes over without a sound. */
function bootstrapCorrections(done) {
  var applyLocal = function (serverList) {
    applySavedCorrections(serverList || savedCorrectionsLocal());
    applyDictionary();
    done();
  };
  if (window.PC_API.USE_MOCK || !S.jobId) { applyLocal(null); return; }
  window.PC_API.getCorrections(S.jobId).then(function (body) {
    S.saveMode = "server";
    S.saveError = false;
    applyLocal((body && body.corrections) || []);
  }).catch(function (err) {
    if (err && err.status === 404) {
      /* Older backend without the route (the job loaded fine): quiet local. */
      S.saveMode = "local";
      applyLocal(null);
      return;
    }
    if (err && err.kind === "down") { applyLocal(null); return; } /* network: quiet local cache */
    /* Server error (500 = saved corrections unreadable): show them the local
       cache, but say plainly the server copy could not be read. */
    S.saveError = true;
    toast("Could not read saved corrections from the server (" + ((err && err.status) || "error") +
      ") · showing the local cache");
    applyLocal(null);
  });
}

/* ---------------- scroll (the only animated thing besides the toggle) ---------------- */

function animateScroll(container, target) {
  var start = container.scrollTop;
  var d = target - start;
  if (Math.abs(d) < 1) return;
  var t0 = performance.now();
  function frame(t) {
    var k = Math.min(1, (t - t0) / MOTION_BASE);
    var e = 1 - Math.pow(1 - k, 3);
    container.scrollTop = start + d * e;
    if (k < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

/* Scroll so the element's middle sits at 1/3 of the container height. */
function scrollToThird(container, node) {
  var cr = container.getBoundingClientRect();
  var er = node.getBoundingClientRect();
  var margin = 8;
  var visible = er.top >= cr.top - margin && er.bottom <= cr.bottom + margin;
  if (visible) return;
  var target = container.scrollTop + (er.top - cr.top) + er.height / 2 - container.clientHeight / 3;
  animateScroll(container, target);
}

/* ---------------- selection ---------------- */

function refreshWordEl(w) {
  if (!w.el) return;
  w.el.className = "w tamil-editor is-" + w.bin + " pv-" + w.prov +
    (w.fixed ? " is-fixed" : "") +
    (w.key === S.activeKey ? " is-active" : "") +
    (w.key === S.hoverKey ? " is-hover" : "");
  if (w.el.textContent !== w.text) w.el.textContent = w.text;
  styleAutoEl(w);
}

/* Auto-applied fixes get a quiet brand-orange outline so they stay
   distinguishable from human-accepted fixes in both lenses. outline is
   used so the hover/active background + box-shadow states still show. */
function styleAutoEl(w) {
  if (w.autoApplied) {
    w.el.style.outline = "1px solid var(--pc-color-primary)";
    w.el.style.outlineOffset = "1px";
    w.el.title = "Auto-applied from a past correction - click again to review or undo";
  } else {
    w.el.style.outline = "";
    w.el.style.outlineOffset = "";
    w.el.title = "";
  }
}

function setActive(key) {
  var prev = S.byKey[S.activeKey];
  S.activeKey = key;
  if (prev) {
    refreshWordEl(prev);
    if (prev.boxEl) prev.boxEl.className = boxClass(prev);
    if (prev.rowEl) prev.rowEl.classList.remove("is-active");
  }
  var w = S.byKey[key];
  if (w) {
    refreshWordEl(w);
    if (w.boxEl) w.boxEl.className = boxClass(w);
    if (w.rowEl) {
      w.rowEl.classList.add("is-active");
      scrollToThird(el.queue, w.rowEl);
    }
  }
}

function select(key, origin) {
  if (S.popupKey && S.popupKey !== key) closePopup();
  setActive(key);
  var w = S.byKey[key];
  if (!w) return;
  if (origin === "word" || origin === "queue") {
    if (w.boxEl) scrollToThird(el.scanScroll, w.boxEl);
  }
  if (origin === "box" || origin === "queue") {
    scrollToThird(el.textScroll, w.el);
  }
}

function clearActive() {
  closePopup();
  setActive(null);
}

function setHover(key) {
  if (S.hoverKey === key) return;
  var prev = S.byKey[S.hoverKey];
  S.hoverKey = key;
  S.hoverLine = key ? S.byKey[key].lineId : null;
  if (prev) {
    refreshWordEl(prev);
    if (prev.boxEl) prev.boxEl.className = boxClass(prev);
  }
  var w = S.byKey[key];
  if (w) {
    refreshWordEl(w);
    if (w.boxEl) w.boxEl.className = boxClass(w);
  }
  var regions = el.paper.querySelectorAll(".box-region");
  for (var i = 0; i < regions.length; i++) {
    var show = regions[i].getAttribute("data-line") === S.hoverLine;
    regions[i].classList.toggle("show", show);
  }
}

/* ---------------- text pane ---------------- */

function renderText() {
  el.textBody.className = "text-body lens-" + S.lens + " mode-" + S.mode;
  el.textRows.innerHTML = "";
  S.lines.forEach(function (line) {
    var row = document.createElement("div");
    row.className = "tline";
    var gutter = document.createElement("span");
    gutter.className = "lid";
    gutter.textContent = line.id;
    var words = document.createElement("div");
    words.className = "twords";
    line.words.forEach(function (w, i) {
      if (i > 0) words.appendChild(document.createTextNode(" "));
      var s = document.createElement("span");
      s.dataset.key = w.key;
      s.tabIndex = -1;
      s.textContent = w.text;
      w.el = s;
      refreshWordEl(w);
      s.addEventListener("click", function () {
        /* Second click on the active word opens the fix popup - the mouse
           path to review or undo an auto-applied fix. */
        if (S.activeKey === w.key) openPopup();
        else select(w.key, "word");
      });
      s.addEventListener("mouseenter", function () { setHover(w.key); });
      s.addEventListener("mouseleave", function () { setHover(null); });
      words.appendChild(s);
    });
    row.appendChild(gutter);
    row.appendChild(words);
    el.textRows.appendChild(row);
  });
}

/* ---------------- scan pane ---------------- */

function boxClass(w) {
  if (w.key === S.activeKey) return "box box-active";
  if (w.key === S.hoverKey) return "box box-hover";
  /* A fixed word is resolved: drop the doubt fill, keep a quiet box so it
     stays clickable on the scan. */
  if (w.fixed) return "box box-auto";
  if (w.bin === "doubt") return "box box-doubt";
  if (w.bin === "auto" && S.mode === "review") return "box box-auto";
  return "box";
}

function renderScan() {
  var paperW = el.paper.clientWidth;
  if (!paperW) return;
  S.scale = paperW / IMG_W;
  var s = S.scale;
  el.paper.style.height = Math.round((S.pageH || PAGE_H) * s) + "px";
  el.paper.innerHTML = "";

  /* Live mode: draw the real scan behind the boxes when the backend's
     scan-image route answers (SCAN_IMAGE in api.js - URL still TBD by the
     backend owner). Probe it once per page; on any failure the placeholder
     paper below stays, so the editor still works. Mock mode never probes. */
  if (S.pageImageUrl && !S.imageTried) {
    S.imageTried = true;
    var probe = new Image();
    probe.onload = function () {
      /* bboxes live in the 1600px-capped space: map the image height into it */
      S.pageH = probe.naturalWidth ? Math.round(probe.naturalHeight * (IMG_W / probe.naturalWidth)) : PAGE_H;
      S.imageLoaded = true;
      renderScan();
    };
    probe.onerror = function () {
      S.imageFailed = true;
      renderScan();
    };
    probe.src = S.pageImageUrl;
  }
  if (S.imageLoaded && S.pageImageUrl) {
    el.paper.style.backgroundImage = "url(\"" + S.pageImageUrl + "\")";
    el.paper.style.backgroundSize = "100% 100%";
    el.paper.style.backgroundRepeat = "no-repeat";
  } else {
    el.paper.style.backgroundImage = "";
  }

  /* Placeholder sheet: each line body placed at its bbox. Skipped once the
     real scan image is on the paper (the text would double-draw). */
  if (!S.imageLoaded) {
  /* Each word is drawn inside its own (approximate) word bbox, so the
     placeholder text lines up with the word boxes drawn on top of it
     instead of running past them. If a word is wider than its box, the
     whole line's type is shrunk evenly to fit; nothing is clipped. */
  S.lines.forEach(function (line) {
    var d = document.createElement("div");
    d.className = "paper-line";
    d.style.left = Math.round(line.bbox[0] * s) + "px";
    d.style.top = Math.round(line.bbox[1] * s) + "px";
    d.style.width = Math.round(line.bbox[2] * s) + "px";
    d.style.height = Math.round(line.bbox[3] * s) + "px";
    d.setAttribute("aria-label", line.body);
    el.paper.appendChild(d);
    var fit = 1;
    line.words.forEach(function (w) {
      var span = document.createElement("span");
      span.className = "paper-word";
      span.setAttribute("aria-hidden", "true");
      span.style.left = Math.round((w.bbox[0] - line.bbox[0]) * s) + "px";
      span.style.width = Math.round(w.bbox[2] * s) + "px";
      span.textContent = w.text;
      d.appendChild(span);
      var room = span.clientWidth;
      var need = span.scrollWidth;
      if (room > 0 && need > room) fit = Math.min(fit, room / need);
    });
    if (fit < 1) {
      var fs = parseFloat(getComputedStyle(d).fontSize) || 18;
      d.style.fontSize = (fs * fit * 0.94).toFixed(2) + "px"; /* small margin: glyph widths do not scale exactly linearly */
    }
  });
  }

  var layer = document.createElement("div");
  layer.className = "scan-layer";

  /* Heatmap fills sit at the bottom (per line: no words[] in the mock). */
  if (S.heat) {
    S.lines.forEach(function (line) {
      var h = document.createElement("div");
      h.className = "box box-heat heat-" + binOf(line.conf);
      h.style.left = Math.round(line.bbox[0] * s - BOX_PAD) + "px";
      h.style.top = Math.round(line.bbox[1] * s - BOX_PAD) + "px";
      h.style.width = Math.round(line.bbox[2] * s + BOX_PAD * 2) + "px";
      h.style.height = Math.round(line.bbox[3] * s + BOX_PAD * 2) + "px";
      layer.appendChild(h);
    });
  }

  /* Line region boxes (revealed on line hover). */
  S.lines.forEach(function (line) {
    var r = document.createElement("div");
    r.className = "box box-region" + (S.hoverLine === line.id ? " show" : "");
    r.setAttribute("data-line", line.id);
    r.style.left = Math.round(line.bbox[0] * s - BOX_PAD) + "px";
    r.style.top = Math.round(line.bbox[1] * s - BOX_PAD) + "px";
    r.style.width = Math.round(line.bbox[2] * s + BOX_PAD * 2) + "px";
    r.style.height = Math.round(line.bbox[3] * s + BOX_PAD * 2) + "px";
    layer.appendChild(r);
  });

  /* Word boxes on top. */
  S.words.forEach(function (w) {
    var cls = boxClass(w);
    if (cls === "box") return; /* ok words draw no box */
    var b = document.createElement("div");
    b.className = cls;
    b.dataset.key = w.key;
    b.setAttribute("role", "button");
    b.setAttribute("aria-label", w.text);
    b.style.left = Math.round(w.bbox[0] * s - BOX_PAD) + "px";
    b.style.top = Math.round(w.bbox[1] * s - BOX_PAD) + "px";
    b.style.width = Math.round(w.bbox[2] * s + BOX_PAD * 2) + "px";
    b.style.height = Math.round(w.bbox[3] * s + BOX_PAD * 2) + "px";
    if (w.autoApplied) b.style.borderColor = "var(--pc-color-primary)";
    b.addEventListener("click", function () { select(w.key, "box"); });
    b.addEventListener("mouseenter", function () { setHover(w.key); });
    b.addEventListener("mouseleave", function () { setHover(null); });
    w.boxEl = b;
    layer.appendChild(b);
  });

  el.paper.appendChild(layer);
  el.heatLegend.hidden = !S.heat;
}

/* ---------------- legend ---------------- */

function renderLegend() {
  el.legend.className = "legend lens-" + S.lens;
  var items = S.lens === "conf"
    ? [["is-auto", "Auto ≥0.90"], ["is-ok", "OK"], ["is-doubt", "Doubt <0.75"]]
    : [["pv-raw", "Raw"], ["pv-rule", "Rule"], ["pv-swap", "Swap"], ["pv-llm", "LLM"], ["pv-human", "Human"]];
  el.legend.innerHTML = "";
  items.forEach(function (it) {
    var item = document.createElement("span");
    item.className = "legend-item";
    var sw = document.createElement("i");
    sw.className = "sw w " + it[0];
    sw.textContent = "Aa";
    var label = document.createElement("span");
    label.textContent = it[1];
    item.appendChild(sw);
    item.appendChild(label);
    el.legend.appendChild(item);
  });
}

/* ---------------- review queue ---------------- */

function renderQueue() {
  el.queue.innerHTML = "";
  S.queue.forEach(function (w) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "qrow" + (w.key === S.activeKey ? " is-active" : "") + (w.fixed ? " is-fixed" : "");
    row.dataset.key = w.key;
    var bar = document.createElement("span");
    bar.className = "qbar";
    var mid = document.createElement("span");
    mid.className = "qmid";
    var word = document.createElement("span");
    word.className = "qword";
    word.textContent = w.text;
    var meta = document.createElement("span");
    meta.className = "qmeta";
    meta.textContent = "p" + w.page + " · " + w.lineId + " · w" + w.idx +
      (w.autoApplied ? " · auto" : (w.prov !== "raw" ? " · " + w.prov : ""));
    mid.appendChild(word);
    mid.appendChild(meta);
    var chip = document.createElement("span");
    if (w.fixed) {
      chip.className = "conf-chip is-done";
      chip.textContent = "done";
    } else {
      chip.className = "conf-chip is-" + w.bin;
      chip.textContent = w.conf.toFixed(2);
    }
    row.appendChild(bar);
    row.appendChild(mid);
    row.appendChild(chip);
    row.addEventListener("click", function () { select(w.key, "queue"); });
    row.addEventListener("mouseenter", function () { setHover(w.key); });
    row.addEventListener("mouseleave", function () { setHover(null); });
    w.rowEl = row;
    el.queue.appendChild(row);
  });
  el.queueEmpty.hidden = S.queue.length > 0;
}

function renderCounts() {
  var total = S.queue.length;
  var fixed = fixedCount();
  var left = total - fixed;
  el.leftPill.textContent = left + " left";
  var scope = S.mode === "auto" ? "doubt words only (Auto)" : "doubt + OK (Review)";
  el.railSub.textContent = fixed + " of " + total + " fixed · " + scope +
    (S.dictCount ? " · " + S.dictCount + " auto-applied" : "");
  var pct = total ? Math.round(fixed / total * 100) : 100;
  el.progressFill.style.width = pct + "%";
  el.progressBar.setAttribute("aria-valuenow", String(pct));
}

/* ---------------- top bar ---------------- */

function renderBadges() {
  var state = pageState();
  el.pageBadges.innerHTML = "";
  var doubtLeft = S.words.filter(function (w) { return w.bin === "doubt" && !w.fixed; }).length;
  var b = document.createElement("button");
  b.type = "button";
  b.className = "pill pill--bar is-" + state + " pill--current";
  /* Label and icon follow the real page state (was hardcoded "Repaired"). */
  var STATE_LABEL = { repaired: "Repaired", clean: "Clean", precomputed: "Precomputed", queued: "Queued" };
  var icon = state === "repaired" ? ICONS.wrench
    : state === "clean" ? ICONS.checkCircle
    : '<i class="pdot" aria-hidden="true"></i>';
  b.innerHTML = icon + "<span></span>";
  b.querySelector("span").textContent = "P" + S.page.page + " · " + (STATE_LABEL[state] || state) +
    " · " + (doubtLeft ? doubtLeft + " doubt" : "no doubt left");
  b.addEventListener("click", function () {
    clearActive();
    el.scanScroll.scrollTop = 0;
    el.textScroll.scrollTop = 0;
    el.queue.scrollTop = 0;
  });
  el.pageBadges.appendChild(b);
  el.scanSub.textContent = "page " + S.page.page + " of " + (S.pageCount || 1) + " · " + S.page.profile + " · " + state;
  var open = state === "clean" || state === "repaired" || state === "precomputed";
  el.exportBtn.disabled = !open;
}

/* ---------------- connection badge (display only) ----------------
   Mock / unreachable backend -> "Offline" (amber dot). Live mode checks
   GET /health once, the same probe the upload page uses; a reachable
   backend -> "Connected" (green dot). Independent of job loading/polling. */
function setConn(state) {
  var badge = $("connBadge");
  var text = $("connText");
  if (!badge || !text) return;
  var LABEL = { offline: "Offline", checking: "Checking…", connected: "Connected" };
  badge.setAttribute("data-state", state);
  text.textContent = LABEL[state] || "Offline";
  badge.title = state === "offline" && !window.PC_API.USE_MOCK
    ? "No response from " + (window.PC_API.API_BASE || location.origin) + "/health"
    : "";
}

function probeConn() {
  if (window.PC_API.USE_MOCK) { setConn("offline"); return; }
  setConn("checking");
  fetch((window.PC_API.API_BASE || "") + "/health", { cache: "no-store" })
    .then(function (res) { return res.ok ? res.json() : null; })
    .then(function (b) { setConn(b && b.ok ? "connected" : "offline"); })
    .catch(function () { setConn("offline"); });
}

/* ---------------- fix popup ---------------- */

function openPopup() {
  var w = S.byKey[S.activeKey];
  if (!w) return;
  S.popupKey = w.key;
  el.pop.innerHTML = "";
  el.pop.classList.remove("above");

  var arrow = document.createElement("span");
  arrow.className = "pop-arrow";

  var head = document.createElement("div");
  head.className = "pop-head";
  var title = document.createElement("span");
  title.className = "eyebrow";
  title.textContent = "Fix word";
  var spacer = document.createElement("span");
  spacer.className = "spacer";
  var chip = document.createElement("span");
  chip.className = "prov-chip pv-" + w.prov;
  var dot = document.createElement("i");
  dot.className = "pdot";
  chip.appendChild(dot);
  chip.appendChild(document.createTextNode(w.tier ? w.prov + " · " + w.tier : w.prov));
  head.appendChild(title);
  head.appendChild(spacer);
  head.appendChild(chip);

  var input = document.createElement("input");
  input.className = "tanglish";
  input.setAttribute("placeholder", "type Tanglish…");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("spellcheck", "false");

  var preview = document.createElement("div");
  preview.className = "pop-preview tamil-h";
  preview.textContent = w.after || "";

  var evidence = document.createElement("div");
  evidence.className = "pop-evidence";
  evidence.textContent = w.autoApplied
    ? "OCR read " + w.orig + " · auto-applied from a past correction"
    : "OCR read " + w.orig + " · " + (w.evidence || "no correction on file");

  var actions = document.createElement("div");
  actions.className = "pop-actions";
  var reject = document.createElement("button");
  reject.type = "button";
  reject.className = "btn btn--outline btn--sm";
  reject.innerHTML = "<span>Reject</span>";
  var rejectKbd = document.createElement("span");
  rejectKbd.className = "kbd";
  rejectKbd.textContent = "Esc";
  reject.appendChild(rejectKbd);
  var accept = document.createElement("button");
  accept.type = "button";
  accept.className = "btn btn--solid btn--sm";
  var acceptLabel = document.createElement("span");
  acceptLabel.textContent = "Accept";
  var acceptKbd = document.createElement("span");
  acceptKbd.className = "kbd";
  acceptKbd.textContent = "↵";
  accept.appendChild(acceptLabel);
  accept.appendChild(acceptKbd);
  actions.appendChild(reject);
  if (w.autoApplied) {
    var undo = document.createElement("button");
    undo.type = "button";
    undo.className = "btn btn--outline btn--sm";
    undo.innerHTML = "<span>Undo auto-fix</span>";
    undo.addEventListener("click", doUndoAuto);
    actions.appendChild(undo);
  }
  actions.appendChild(accept);

  el.pop.appendChild(arrow);
  el.pop.appendChild(head);
  el.pop.appendChild(input);
  el.pop.appendChild(preview);
  el.pop.appendChild(evidence);
  el.pop.appendChild(actions);

  input.addEventListener("input", function () {
    preview.textContent = window.PC_Translit.transliterate(input.value);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); doAccept(); }
    else if (e.key === "Escape") { e.preventDefault(); doReject(); }
  });
  reject.addEventListener("click", doReject);
  accept.addEventListener("click", doAccept);

  placePopup(w, arrow);
  input.focus();
}

function placePopup(w, arrow) {
  el.pop.hidden = false;
  var bodyW = el.textBody.clientWidth;
  var popW = el.pop.offsetWidth || 288; /* --pc-popup-w, border-box */
  var maxLeft = Math.max(8, bodyW - popW - 8);
  var left = w.el.offsetLeft + w.el.offsetWidth / 2 - popW / 2;
  left = Math.max(8, Math.min(maxLeft, left));
  var popH = el.pop.offsetHeight;
  var viewTop = el.textScroll.scrollTop;
  var viewBottom = viewTop + el.textScroll.clientHeight;
  var below = w.el.offsetTop + w.el.offsetHeight + 8;
  var top;
  if (below + popH <= viewBottom - 8) {
    top = below;
    el.pop.classList.remove("above");
  } else {
    top = w.el.offsetTop - popH - 8;
    el.pop.classList.add("above");
    if (top < 8) top = 8;
  }
  el.pop.style.left = Math.round(left) + "px";
  el.pop.style.top = Math.round(top) + "px";
  var ax = w.el.offsetLeft + w.el.offsetWidth / 2 - left;
  ax = Math.max(12, Math.min(popW - 24, ax));
  arrow.style.left = Math.round(ax) + "px";
}

function closePopup() {
  el.pop.hidden = true;
  S.popupKey = null;
}

function doAccept() {
  var w = S.byKey[S.popupKey];
  if (!w) { closePopup(); return; }
  var preview = el.pop.querySelector(".pop-preview");
  var tamil = preview ? preview.textContent.trim() : "";
  if (!tamil) { closePopup(); return; }
  w.text = tamil;
  w.prov = "human";
  w.target = false;
  w.fixed = true;
  w.autoApplied = false;
  w.autoPrev = null;
  refreshWordEl(w);
  retext();
  /* Save-back (schema/corrections-endpoint.md): debounced PUT of the full
     correction map to the live route; localStorage is the fallback cache for
     network failure / older backends (404) only. The fix also teaches the
     dictionary so the next scan can auto-apply it. */
  recordCorrection(w);
  dictAdd(w.orig, w.text);
  closePopup();
  renderScan();
  renderQueue();
  renderCounts();
  renderBadges();
  toast("Fix accepted · " + tamil + " marked human");
  if (w.el) w.el.focus();
}

function doReject() {
  var w = S.byKey[S.popupKey];
  closePopup();
  if (w && w.el) w.el.focus();
}

/* Revert one auto-applied fix: the OCR text comes back, the word rejoins
   the queue, and a per-job skip stops the dictionary from re-applying it. */
function doUndoAuto() {
  var w = S.byKey[S.popupKey];
  closePopup();
  if (!w || !w.autoApplied) return;
  var prev = w.autoPrev || {};
  w.text = w.orig;
  w.after = prev.after || "";
  w.target = !!prev.target;
  w.prov = prev.prov || "raw";
  w.tier = prev.tier || "";
  w.evidence = prev.evidence || "";
  w.fixed = false;
  w.autoApplied = false;
  w.autoPrev = null;
  skipAdd(w.key);
  if (S.dictCount > 0) S.dictCount--;
  retext();
  refreshWordEl(w);
  renderScan();
  renderQueue();
  renderCounts();
  renderBadges();
  toast("Auto-fix undone · " + w.orig + " restored");
  if (w.el) w.el.focus();
}

/* ---------------- toast ---------------- */

var toastTimer = null;

function toast(msg) {
  el.toast.innerHTML = ICONS.check + "<span></span>";
  el.toast.querySelector("span").textContent = msg;
  el.toast.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    el.toast.classList.remove("show");
  }, TOAST_MS);
}

/* ---------------- toggles ---------------- */

function wireSeg(node, onChange) {
  var opts = Array.prototype.slice.call(node.querySelectorAll(".seg-opt"));
  function set(i, focus) {
    opts.forEach(function (o, j) {
      o.setAttribute("aria-checked", j === i ? "true" : "false");
      o.tabIndex = j === i ? 0 : -1;
    });
    node.setAttribute("data-active", String(i));
    onChange(opts[i].getAttribute("data-val"));
    if (focus) opts[i].focus();
  }
  node.addEventListener("click", function (e) {
    var o = e.target.closest(".seg-opt");
    if (o) set(opts.indexOf(o), false);
  });
  node.addEventListener("keydown", function (e) {
    var cur = opts.indexOf(document.activeElement);
    var n = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") n = (cur + 1) % opts.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") n = (cur - 1 + opts.length) % opts.length;
    else if (e.key === "Home") n = 0;
    else if (e.key === "End") n = opts.length - 1;
    if (n !== null && n !== undefined) {
      e.preventDefault();
      set(n, true);
    }
  });
}

/* ---------------- hotkeys ---------------- */

function stepQueue(dir) {
  if (!S.queue.length) { toast("Queue done"); return; }
  var i = -1;
  for (var k = 0; k < S.queue.length; k++) {
    if (S.queue[k].key === S.activeKey) { i = k; break; }
  }
  var n = dir > 0 ? i + 1 : i - 1;
  if (i === -1) n = dir > 0 ? 0 : S.queue.length - 1;
  while (n >= 0 && n < S.queue.length && S.queue[n].fixed) n += dir;
  if (n < 0 || n >= S.queue.length) {
    toast(dir > 0 ? "Queue done" : "Start of queue");
    return;
  }
  select(S.queue[n].key, "queue");
}

function wireHotkeys() {
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    var inField = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA");
    if (S.popupKey) {
      if (inField) return; /* the Tanglish input handles its own keys */
      if (t && t.closest && t.closest("button")) {
        if (e.key === "Escape") { e.preventDefault(); doReject(); }
        return; /* Enter on a focused button clicks it natively */
      }
      if (e.key === "Enter") { e.preventDefault(); doAccept(); }
      else if (e.key === "Escape") { e.preventDefault(); doReject(); }
      return;
    }
    if (inField) return;
    var key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (key === "j") { e.preventDefault(); stepQueue(1); }
    else if (key === "k") { e.preventDefault(); stepQueue(-1); }
    else if (key === "Enter") {
      if (S.activeKey) { e.preventDefault(); openPopup(); }
    }
    else if (key === "Escape") { clearActive(); }
  });
}

/* ---------------- load states (live mode) ---------------- */

var overlayEl = null;

function hideOverlay() {
  if (overlayEl && overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
  overlayEl = null;
}

/* Full-pane overlay for loading / processing / fatal states. Covers the
   panes so the three empty panes never read as a broken editor. */
function showOverlay(title, detail) {
  hideOverlay();
  overlayEl = document.createElement("div");
  overlayEl.className = "pc-load-overlay";
  overlayEl.style.position = "absolute";
  overlayEl.style.inset = "0";
  overlayEl.style.zIndex = "30";
  overlayEl.style.display = "flex";
  overlayEl.style.flexDirection = "column";
  overlayEl.style.alignItems = "center";
  overlayEl.style.justifyContent = "center";
  overlayEl.style.gap = "var(--pc-gap-10)";
  overlayEl.style.padding = "var(--pc-space-6)";
  overlayEl.style.textAlign = "center";
  overlayEl.style.background = "var(--pc-color-bg-canvas)";
  overlayEl.setAttribute("role", "status");
  overlayEl.setAttribute("aria-live", "polite");
  var t = document.createElement("div");
  t.className = "pc-load-title";
  t.style.fontWeight = "600";
  t.style.fontSize = "var(--pc-fs-btn)";
  t.style.color = "var(--pc-color-text-primary)";
  t.textContent = title;
  var d = document.createElement("div");
  d.className = "pc-load-detail";
  d.style.color = "var(--pc-color-text-secondary)";
  d.style.maxWidth = "52ch";
  d.style.fontSize = "var(--pc-fs-toast)";
  d.style.lineHeight = "1.5";
  d.style.overflowWrap = "anywhere";
  d.textContent = detail;
  overlayEl.appendChild(t);
  /* Visually hidden separator: keeps the title and detail from running
     together in textContent, copied text and screen readers
     ("Job still processingJob ..."). Out of flow, so the layout is unchanged. */
  var sep = document.createElement("span");
  sep.style.position = "absolute";
  sep.style.width = "1px";
  sep.style.height = "1px";
  sep.style.overflow = "hidden";
  sep.style.clipPath = "inset(50%)";
  sep.style.whiteSpace = "nowrap";
  sep.textContent = ". ";
  overlayEl.appendChild(sep);
  overlayEl.appendChild(d);
  var host = document.querySelector(".panes");
  if (getComputedStyle(host).position === "static") host.style.position = "relative";
  host.appendChild(overlayEl);
}

/* Clear, actionable failure: what happened + how to still see the demo. */
function showFatal(err) {
  hideOverlay();
  var hint = window.PC_API.USE_MOCK
    ? ""
    : " Start the backend (schema/endpoints.md) or open editor.html?mock=1 for the offline Kural demo.";
  showOverlay("Could not open this job", err.message + "." + hint);
  toast("Could not load job · " + err.message);
}

/* ---------------- boot ---------------- */

var BOOT_QS = new URLSearchParams(location.search);
var JOB_ID = BOOT_QS.get("job");
var PAGE_NO = parseInt(BOOT_QS.get("page") || "1", 10);
if (!(PAGE_NO >= 1)) PAGE_NO = 1;

function renderAll() {
  renderText();
  renderScan();
  renderLegend();
  renderQueue();
  renderCounts();
  renderBadges();
}

function loadPage(doc) {
  buildModel(doc);
  bootstrapCorrections(function () {
    renderAll();
    if (S.dictCount) {
      toast(S.dictCount + (S.dictCount === 1 ? " fix" : " fixes") + " auto-applied from past corrections");
    }
  });
}

function init() {
  AUTO_MIN = cssNum("--pc-conf-auto-min", 0.9);
  OK_MIN = cssNum("--pc-conf-ok-min", 0.75);
  MOTION_BASE = cssNum("--pc-motion-base", 160);
  TOAST_MS = cssNum("--pc-toast-duration", 2400);

  el.pageBadges = $("pageBadges");
  el.exportBtn = $("exportBtn");
  el.scanSub = $("scanSub");
  el.scanScroll = $("scanScroll");
  el.paper = $("paper");
  el.heatLegend = $("heatLegend");
  el.textScroll = $("textScroll");
  el.textBody = $("textBody");
  el.textRows = $("textRows");
  el.pop = $("fixPop");
  el.legend = $("legend");
  el.leftPill = $("leftPill");
  el.railSub = $("railSub");
  el.progressBar = $("progressBar");
  el.progressFill = $("progressFill");
  el.queue = $("queue");
  el.queueEmpty = $("queueEmpty");
  el.toast = $("toast");

  wireSeg($("modeToggle"), function (val) {
    if (S.mode === val) return;
    S.mode = val;
    closePopup();
    rebuildQueue();
    renderText();
    renderScan();
    renderQueue();
    renderCounts();
  });
  wireSeg($("heatToggle"), function (val) {
    S.heat = val === "on";
    renderScan();
  });
  wireSeg($("lensToggle"), function (val) {
    if (S.lens === val) return;
    S.lens = val;
    el.textBody.classList.remove("lens-conf", "lens-prov");
    el.textBody.classList.add("lens-" + val);
    renderLegend();
  });

  el.exportBtn.addEventListener("click", function () {
    if (!S.page) return;
    /* Mock mode: unchanged demo toast. */
    if (window.PC_API.USE_MOCK || !S.jobId) {
      toast("Export ready · page " + S.page.page);
      return;
    }
    /* Flush any pending debounced save BEFORE exporting: the PDF is built
       from the server's correction map, and a fix accepted in the last
       800ms would otherwise be missing from it. flushSave never throws. */
    flushSave().then(function () {
      if (S.saveMode === "local") {
        /* This backend predates the corrections route (or is unreachable), so
           its export would use raw OCR. Apply the corrections client-side to
           this page's text and hand it over as a download instead. */
        var blob = new Blob([S.page.text + "\n"], { type: "text/plain;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = "pink-cloud-" + S.jobId.slice(0, 8) + "-p" + S.page.page + "-corrected.txt";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        toast("Exported with corrections applied locally");
        return;
      }
      /* A server-side save/read failed earlier (500 etc.): the PDF is built
         from the server's correction map, so it may miss the latest fixes. */
      if (S.saveError) {
        toast("Warning: the latest fixes were not saved on the server · the PDF may not include them");
      }
      /* Live mode: open the backend's searchable-PDF export for the whole job
         (GET /jobs/{job_id}/export.pdf, same origin unless ?api= overrides).
         New tab: the browser shows or downloads the PDF. */
      window.open(window.PC_API.API_BASE + "/jobs/" + encodeURIComponent(S.jobId) + "/export.pdf",
        "_blank", "noopener");
    });
  });

  wireHotkeys();
  probeConn();

  var raf = 0;
  window.addEventListener("resize", function () {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      if (S.page) renderScan();
    });
  });

  bootData();
}

/* Wraps loadPage with the job context the live mode needs (page count for
   the header, scan-image URL, the state flags renderScan probes). */
function loadJob(doc, meta) {
  S.jobId = meta.jobId || null;
  S.pageCount = meta.pageCount || 1;
  S.pageImageUrl = (meta.jobId && window.PC_API.pageImageUrl)
    ? window.PC_API.pageImageUrl(meta.jobId, doc.page)
    : null;
  S.imageTried = false;
  S.imageLoaded = false;
  S.imageFailed = false;
  S.pageH = null;
  S.corrections = {};
  S.saveMode = "unknown";
  S.saveError = false;
  S.corrDirty = false;
  S.dictCount = 0;
  saveWarned = false;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  loadPage(doc);
}

/* Live mode: GET /jobs/{job_id}; poll while status is "pending" (POST /jobs
   is synchronous today, but the contract allows background processing). */
function pollJob(depth) {
  window.PC_API.getJob(JOB_ID).then(function (job) {
    if (job.status === "pending") {
      if (depth >= window.PC_API.POLL_MAX) {
        showFatal(new Error("Timed out waiting for job " + JOB_ID + " - still processing on the server"));
        return;
      }
      showOverlay("Job still processing",
        "Job " + JOB_ID + " is still running on the backend - checking again every " +
        (window.PC_API.POLL_MS / 1000) + "s.");
      setTimeout(function () { pollJob(depth + 1); }, window.PC_API.POLL_MS);
      return;
    }
    if (job.status === "error") {
      showFatal(new Error("Job " + JOB_ID + " failed on the backend: " +
        ((job.result && job.result.error) || "unknown error")));
      return;
    }
    var pages = job.result && job.result.pages;
    if (!pages || !pages.length) {
      showFatal(new Error("Job " + JOB_ID + " returned no pages"));
      return;
    }
    var idx = Math.min(PAGE_NO, pages.length) - 1;
    hideOverlay();
    loadJob(pages[idx], { jobId: JOB_ID, pageCount: pages.length });
  }).catch(showFatal);
}

function bootData() {
  if (window.PC_API.USE_MOCK) {
    /* Demo path, untouched: the Kural page from schema/doc_demo.json. */
    window.PC_API.getPage(1)
      .then(function (doc) { loadJob(doc, {}); })
      .catch(function (err) { toast("Could not load page · " + err.message); });
    return;
  }
  if (!JOB_ID) {
    showFatal(new Error("No job id in the URL - open editor.html?job=<job_id> after an upload"));
    return;
  }
  showOverlay("Loading job", "GET /jobs/" + JOB_ID + " on " + (window.PC_API.API_BASE || "this origin"));
  pollJob(0);
}

document.addEventListener("DOMContentLoaded", init);
})();

