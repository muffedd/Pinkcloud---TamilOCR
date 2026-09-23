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

/* Bands on the text-quality proxy scale. The Sarvam engine gives no per-line
   recognition confidence (line.confidence is a layout-block score), so the
   editor scores each live line itself with textScore() below, a port of
   app/textcheck.py score_line(text): clean Tamil 0.99; flaws (orphan vowel
   signs, odd characters, repeats) 0.90 sliding to 0.62; garbage <= 0.54; low
   Tamil share slides toward 0; digits-only 0.3. Values come from tokens.css
   (--pc-conf-*); these are fallbacks.
     Auto  >= 0.95 : clean text
     OK    0.80-0.95: one or two small flaws (queued in Review mode)
     Doubt < 0.80  : several flaws, low Tamil share, garbage */
var AUTO_MIN = 0.95;
var OK_MIN = 0.80;
/* Review floor for pages the backend routed to review (needs_review, or
   profile HEAVY): any line that is not clean (< 0.95, i.e. a single flaw at
   0.90 or worse) is Doubt. Clean pages keep the normal bands. The proxy only
   sees malformed text: a wrong but well-formed Tamil word scores 0.99 and is
   NOT flagged. This raises recall on broken output, it is not proof of
   correctness. */
var REVIEW_FLOOR = 0.95;
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

/* True when the backend flagged this page for review (HEAVY implies it in
   schema_out.py; checked too in case an older result lacks the flag). */
function isReviewPage(doc) {
  return !!doc && (doc.needs_review === true || doc.profile === "HEAVY");
}

/* Review-queue bin for one line: the normal bands, raised to Doubt below
   REVIEW_FLOOR on review pages. Only reads contract fields. */
function reviewBinOf(conf, doc) {
  if (isReviewPage(doc) && conf < REVIEW_FLOOR) return "doubt";
  return binOf(conf);
}

/* ---------------- text check (port of fastapi/sqlite/app/textcheck.py) ----------------
   Live pages: Sarvam's line.confidence is a LAYOUT-BLOCK score (one value per
   block, unrelated to how the text reads), so the editor does not bin or show
   it. Each line is scored from its OCR text instead - the agreed text-quality
   proxy scale: ~0.99 clean Tamil, ~0.90 one flaw (orphan vowel sign, odd
   char, repeated sentence), <= 0.6 low Tamil share / garbage, 0.3 digits or
   punctuation only, 0 empty. Keep in sync with textcheck.score_line; the
   parity check (every line of the committed Sarvam samples) is in the
   commit message. The offline mock/fixture pages keep their own confidence. */
var TC = {
  CLEAN: 0.99, LOW_CAP: 0.6, SHARE_OK: 0.95, SHARE_MIN: 0.5,
  FIRST_FLAW: 0.09, EXTRA_FLAW: 0.04, FLAW_FLOOR: 0.62, GARBAGE_RATIO: 0.2
};
var TC_TYPO = "\u2010\u2011\u2012\u2013\u2014\u2015\u2018\u2019\u201c\u201d\u2022\u2026\u00ab\u00bb\u00b7\u00a0";
var TC_LETTER = /\p{L}/u, TC_CN = /\p{Cn}/u, TC_SPACE = /\s/;
function tcSign(c) { return (c >= 0x0BBE && c <= 0x0BCC) || c === 0x0BD7; }
function tcCons(c) { return c >= 0x0B95 && c <= 0x0BB9; }

function textScore(text) {
  text = text == null ? "" : String(text);
  if (!text.trim()) return 0;
  var chars = Array.from(text);            /* code points, like Python str */
  var letters = 0, tamil = 0, visible = 0, orphans = 0, odd = 0;
  for (var i = 0; i < chars.length; i++) {
    var ch = chars[i], code = ch.codePointAt(0);
    var space = TC_SPACE.test(ch);
    if (!space) visible++;
    if (TC_LETTER.test(ch)) { letters++; if (code >= 0x0B80 && code <= 0x0BFF) tamil++; }
    if (tcSign(code) || code === 0x0BCD) {
      var prev = i ? chars[i - 1].codePointAt(0) : null;
      if (prev === null || !(tcCons(prev) || tcSign(prev))) orphans++;
    }
    if (space || ch === "\u200c" || ch === "\u200d" || TC_TYPO.indexOf(ch) !== -1) continue;
    if (code >= 0x0B80 && code <= 0x0BFF) { if (TC_CN.test(ch)) odd++; continue; }
    if (code < 0x80) { if (code < 0x20 || code === 0x7F) odd++; continue; }
    if (TC_LETTER.test(ch)) continue;
    odd++;
  }
  /* repeated sentences + word repetition loop */
  var seen = {}, repeated = 0;
  (text.match(/[^.!?\u0964\u0965]+[.!?\u0964\u0965]*/g) || []).forEach(function (m) {
    var k = m.trim();
    if (!k) return;
    if (seen[k]) repeated++; else seen[k] = true;
  });
  var words = text.match(/\S+/g) || [], loop = false;
  if (words.length >= 4) {
    var cnt = {}, top = 0;
    words.forEach(function (w) { cnt[w] = (cnt[w] || 0) + 1; if (cnt[w] > top) top = cnt[w]; });
    loop = top >= 3 && top / words.length > 0.5;
  }
  var share = letters ? tamil / letters : null, base;
  if (share === null) base = 0.3;
  else if (share >= TC.SHARE_OK) base = TC.CLEAN;
  else if (share >= TC.SHARE_MIN) base = 0.75 + (TC.CLEAN - 0.75) * (share - TC.SHARE_MIN) / (TC.SHARE_OK - TC.SHARE_MIN);
  else base = TC.LOW_CAP * share / TC.SHARE_MIN;
  var flaws = orphans + odd + repeated, score = base;
  if (flaws) score = Math.max(base - TC.FIRST_FLAW - TC.EXTRA_FLAW * (flaws - 1), Math.min(base, TC.FLAW_FLOOR));
  if (odd / Math.max(visible, 1) > TC.GARBAGE_RATIO || loop) score = Math.min(score, TC.LOW_CAP * 0.9);
  return Math.round(Math.max(0, Math.min(1, score)) * 10000) / 10000;
}

/* The number every band, heat colour and chip uses for a line. */
function lineScore(line) {
  if (window.PC_API && window.PC_API.USE_MOCK) return line.confidence;
  return textScore(line.body);
}

function buildModel(doc) {
  S.page = doc;
  S.lines = [];
  S.words = [];
  S.byKey = {};
  var corrections = doc.corrections || [];
  var ordered = doc.lines.slice().sort(function (a, b) { return a.seq - b.seq; });
  ordered.forEach(function (line) {
    var score = lineScore(line);
    var entry = { id: line.id, seq: line.seq, body: line.body, bbox: line.bbox, conf: score, words: [] };
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
        conf: score,
        bin: reviewBinOf(score, doc),
        prov: hit ? (TIER_PROV[hit.tier] || "raw") : "raw",
        tier: hit ? hit.tier : "",
        evidence: hit ? hit.evidence : "",
        target: !!hit,
        bbox: bbox,
        approx: true,
        fixed: false,
        autoApplied: false,  /* text came from the corrections dictionary, not the reviewer */
        autoPrev: null,      /* pre-auto-apply suggestion state, for undo */
        sugg: [],            /* ranked candidates: [{text, score, source}] (attachSuggestions) */
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
  attachSuggestions(doc);
  rebuildQueue();
}

/* ---------------- suggestions (contract: schema/suggestions-contract.md) ----------------
   page.suggestions[] = [{line, word, before, candidates: [{text, score, source}]}]
     line   - line id ("L3"), matches lines[].id
     word   - 1-based word index in that line's body split on whitespace (the
              same index the corrections endpoint uses)
     before - the word text the candidates were computed for; if it no longer
              matches the OCR word the entry is ignored (stale)
     candidates - best first; the editor sorts by score, drops duplicates and
              the OCR word itself, and keeps at most 9 (number keys 1-9).
   A page correction (corrections[], matched by `before`) becomes candidate 1.
   Optional field: pages without it simply have no suggestions. */
var MAX_SUGG = 9;
var CORR_SOURCE = { T1: "swap", T2: "rule", T3: "llm" };

function attachSuggestions(doc) {
  var bySlot = {};
  (Array.isArray(doc.suggestions) ? doc.suggestions : []).forEach(function (sg) {
    if (!sg || typeof sg.line !== "string" || !(sg.word >= 1) || !Array.isArray(sg.candidates)) return;
    bySlot["p" + doc.page + ":" + sg.line + ":w" + sg.word] = sg;
  });
  S.words.forEach(function (w) {
    var list = [];
    var seen = {};
    function add(text, score, source) {
      text = String(text || "").trim();
      if (!text || text === w.orig || seen[text]) return;
      seen[text] = true;
      list.push({ text: text, score: typeof score === "number" ? score : null, source: source || "" });
    }
    if (w.target && w.after) add(w.after, null, CORR_SOURCE[w.tier] || "correction");
    var sg = bySlot[w.key];
    if (sg && (sg.before == null || sg.before === w.orig)) {
      sg.candidates.slice().sort(function (a, b) {
        return (typeof b.score === "number" ? b.score : -1) - (typeof a.score === "number" ? a.score : -1);
      }).forEach(function (c) { if (c) add(c.text, c.score, c.source); });
    }
    w.sugg = list.slice(0, MAX_SUGG);
    /* Prefill the fix popup with the best candidate when no correction is on file. */
    if (!w.after && w.sugg.length) w.after = w.sugg[0].text;
  });
}

/* Routing summary: a line is routed to the reviewer when its band is in the
   current queue scope (Auto mode: Doubt; Review mode: Doubt + OK). Derived
   only from the line's text-check score (lineScore) and the page's
   needs_review / profile. */
function routingSummary() {
  var total = S.lines.length;
  var routed = S.lines.filter(function (l) {
    var bin = reviewBinOf(l.conf, S.page);
    return S.mode === "auto" ? bin === "doubt" : bin !== "auto";
  }).length;
  return { total: total, routed: routed, auto: total - routed };
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
  /* The editor only loads a page once its job is done, so a page with its
     lines present is finished. The backend never sends doc.preprocessed for
     HEAVY pages, so a loaded HEAVY page counts as repaired (exportable). */
  var loaded = Array.isArray(doc.lines);
  if (doc.profile === "HEAVY" && (doc.preprocessed || loaded)) return "repaired";
  if (doc.profile === "FAST" && !doc.needs_review) return "clean";
  if (doc.preprocessed) return "precomputed";
  if (loaded) return "ready"; /* finished, just not repaired/clean: never "Queued" */
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
  return S.jobId || (window.PC_API.USE_MOCK ? (S.mockId || MOCK_STORAGE_ID) : null);
}

/* Full-map payload, sorted into reading order (page, line, word). */
function corrPayload() {
  /* PUT /corrections REPLACES the job's whole map, but this editor only holds
     one page. Carry the other pages' saved entries through untouched, so a
     save on page 2 never wipes page 1's fixes (and page 1's fixes never land
     on page 2's words: keys are page-scoped). */
  var list = (S.otherCorr || []).slice().concat(
    Object.keys(S.corrections).map(function (k) { return S.corrections[k]; }));
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
  S.otherCorr = [];
  if (!list || !list.length) return;
  var touched = false;
  var here = S.page ? Number(S.page.page) : null;
  list.forEach(function (c) {
    if (Number(c.page) !== here) { S.otherCorr.push(c); return; } /* another page's fix: keep, don't apply */
    var w = S.byKey["p" + c.page + ":" + c.line + ":w" + c.word];
    if (!w) return;
    /* Same rule as export.apply_corrections: `before` must be the RAW OCR
       word. A stale entry (before != this word's OCR text) is skipped, never
       applied - so the editor shows what export/search/count use, and the
       next save drops it instead of rewriting it with a fresh before. */
    if (c.before != null && String(c.before) !== w.orig) return;
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
    recordCorrection(w); /* persist auto-fixes like manual ones so export.pdf gets them */
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
    if (prev.cardEl) prev.cardEl.classList.remove("is-active");
  }
  var w = S.byKey[key];
  if (w) {
    refreshWordEl(w);
    if (w.boxEl) w.boxEl.className = boxClass(w);
    if (w.rowEl) w.rowEl.classList.add("is-active");
    if (w.cardEl) w.cardEl.classList.add("is-active");
    if (w.cardEl || w.rowEl) scrollToThird(el.queue, w.cardEl || w.rowEl);
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

/* ---------------- heatmap color ----------------
   The contract has no per-line damage field, so damage is derived from the
   line's text-check score (lineScore). Two signals are combined:
     abs - fixed scale: conf >= HEAT_CLEAN -> 0, conf <= HEAT_BAD -> 1
     rel - where the line sits in this page's own score spread; only
           counts in proportion to how wide that spread is, so a clean page
           with a tiny spread does not paint its "worst" line red.
   0 = clean (green), 0.5 = rough (orange), 1 = damaged (red). */
var HEAT_CLEAN = 0.98;  /* proxy scale: clean Tamil is 0.99 -> green */
var HEAT_BAD = 0.60;    /* flaw floor 0.62 / garbage 0.54 -> red */
var HEAT_SPREAD_FULL = 0.15;

function heatConfRange(lines) {
  var lo = Infinity, hi = -Infinity;
  lines.forEach(function (l) {
    if (typeof l.conf !== "number" || isNaN(l.conf)) return;
    if (l.conf < lo) lo = l.conf;
    if (l.conf > hi) hi = l.conf;
  });
  return lo <= hi ? { lo: lo, hi: hi } : null;
}

function heatDamage(line, range) {
  var c = line.conf;
  if (typeof c !== "number" || isNaN(c)) return 1;
  var abs = (HEAT_CLEAN - c) / (HEAT_CLEAN - HEAT_BAD);
  abs = Math.max(0, Math.min(1, abs));
  var rel = 0;
  if (range && range.hi - range.lo > 0.02) {
    var spread = range.hi - range.lo;
    rel = ((range.hi - c) / spread) * Math.min(1, spread / HEAT_SPREAD_FULL);
  }
  return Math.max(abs, Math.max(0, Math.min(1, rel)));
}

/* green (hue 130) -> orange (hue 30) at 0.5 -> red (hue 0) at 1 */
function heatColor(d, alpha) {
  var hue = d <= 0.5 ? 130 - 200 * d : 60 - 60 * d;
  return "hsla(" + Math.round(hue) + ", 85%, 45%, " + alpha.toFixed(2) + ")";
}

function renderScan() {
  var paperW = el.paper.clientWidth;
  if (!paperW) return;
  /* bboxes live in the rendered page's pixel space. The backend caps the
     LONG side at 1600px (pdfutil MAX_SIDE), so a portrait page is e.g.
     1200x1600 - the width is NOT always 1600. Once the scan image is loaded,
     its natural size IS the bbox space; before that (and in mock mode) the
     1600x1400 placeholder space applies. Every box, the heatmap and
     click-hit-testing all use S.scale, so they follow this together. */
  S.scale = paperW / (S.coordW || IMG_W);
  el.paper.style.height = Math.round((S.pageH || PAGE_H) * S.scale) + "px";
  /* Setting the height can add the scan pane's vertical scrollbar (a tall
     portrait page), which narrows the paper. The background image stretches
     to the new width, so re-read it and rescale or the boxes drift. */
  var paperW2 = el.paper.clientWidth;
  if (paperW2 && paperW2 !== paperW) {
    paperW = paperW2;
    S.scale = paperW / (S.coordW || IMG_W);
    el.paper.style.height = Math.round((S.pageH || PAGE_H) * S.scale) + "px";
  }
  S.renderedW = paperW;
  var s = S.scale;
  el.paper.innerHTML = "";

  /* Live mode: draw the real scan behind the boxes (SCAN_IMAGE in api.js,
     GET /jobs/{id}/pages/{n}/image). Probe it once per page; on any failure
     the placeholder paper below stays, so the editor still works. Mock mode
     never probes. */
  if (S.pageImageUrl && !S.imageTried) {
    S.imageTried = true;
    var probe = new Image();
    probe.onload = function () {
      /* The rendered PNG's pixel size is the bbox coordinate space. */
      S.coordW = probe.naturalWidth || IMG_W;
      S.pageH = probe.naturalHeight || PAGE_H;
      S.imageLoaded = true;
      renderScan();
      renderQueue(); /* queue crops need the image and its size */
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
    var confRange = heatConfRange(S.lines);
    S.lines.forEach(function (line) {
      var h = document.createElement("div");
      var dmg = heatDamage(line, confRange);
      h.className = "box box-heat";
      h.style.background = heatColor(dmg, 0.16 + 0.30 * dmg);
      h.style.borderColor = heatColor(dmg, 0.85);
      h.title = "Damage " + Math.round(dmg * 100) + "% (text check " +
        (typeof line.conf === "number" ? line.conf.toFixed(2) : "?") + ")";
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
  var doubtLabel = isReviewPage(S.page)
    ? "Doubt <" + REVIEW_FLOOR + " (review page)"
    : "Doubt <" + OK_MIN.toFixed(2);
  var items = S.lens === "conf"
    ? [["is-auto", "Auto ≥" + AUTO_MIN.toFixed(2)], ["is-ok", "OK"], ["is-doubt", doubtLabel]]
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
    var card = document.createElement("div");
    card.className = "qcard" + (w.key === S.activeKey ? " is-active" : "") + (w.fixed ? " is-fixed" : "");
    card.dataset.key = w.key;
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
      (w.autoApplied ? " · auto" : (w.prov !== "raw" ? " · " + w.prov : "")) +
      (!w.fixed && w.sugg.length ? " · " + w.sugg.length + (w.sugg.length === 1 ? " suggestion" : " suggestions") : "");
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

    /* Card body: the line's scan crop (+ suggestions on the active card). */
    var body = document.createElement("div");
    body.className = "qcard-body";
    var crop = document.createElement("div");
    crop.className = "qcrop";
    crop.setAttribute("role", "img");
    crop.setAttribute("aria-label", "Scan crop of " + w.lineId + ", word " + w.idx);
    body.appendChild(crop);
    if (w.sugg.length && !w.fixed) {
      var list = document.createElement("div");
      list.className = "qsugg";
      list.setAttribute("role", "group");
      list.setAttribute("aria-label", "Suggestions - press 1 to " + w.sugg.length);
      w.sugg.forEach(function (c, i) { list.appendChild(suggButton(w, c, i)); });
      body.appendChild(list);
    }
    card.appendChild(row);
    card.appendChild(body);

    function pick() { select(w.key, "queue"); }
    row.addEventListener("click", pick);
    crop.addEventListener("click", pick);
    card.addEventListener("mouseenter", function () { setHover(w.key); });
    card.addEventListener("mouseleave", function () { setHover(null); });
    w.rowEl = row;
    w.cardEl = card;
    w.cropEl = crop;
    el.queue.appendChild(card);
  });
  el.queueEmpty.hidden = S.queue.length > 0;
  layoutCrops();
}

/* One numbered suggestion button (queue card and fix popup share it). */
function suggButton(w, c, i) {
  var b = document.createElement("button");
  b.type = "button";
  b.className = "qs-opt";
  b.tabIndex = -1;
  var k = document.createElement("span");
  k.className = "kbd";
  k.textContent = String(i + 1);
  var t = document.createElement("span");
  t.className = "qs-text";
  t.textContent = c.text;
  b.appendChild(k);
  b.appendChild(t);
  if (c.source || typeof c.score === "number") {
    var src = document.createElement("span");
    src.className = "qs-src";
    src.textContent = (c.source || "") + (typeof c.score === "number" ? (c.source ? " " : "") + c.score.toFixed(2) : "");
    b.appendChild(src);
  }
  b.title = "Use " + c.text + " (" + (i + 1) + ")";
  b.addEventListener("click", function (e) {
    e.stopPropagation();
    pickSuggestion(w, i);
  });
  return b;
}

/* ---------------- queue crops ----------------
   Each card shows the word in its line context, cut from the page scan with
   CSS background-position on the EXISTING page image
   (GET /jobs/{id}/pages/{n}/image - the same URL the scan pane uses; the
   browser caches it, no new endpoint). The image's natural size is the bbox
   space (see renderScan). The crop window is centred on the word, as tall as
   its line plus a margin, and as wide as the card's aspect ratio allows. The
   word itself gets a thin outline. Without a page image (offline demo, or the
   image route failed) the crop shows the OCR word on the placeholder paper. */
var CROP_MARGIN = 0.35; /* extra line height above + below, as a share of it */

function layoutCrops() {
  var first = null;
  for (var i = 0; i < S.queue.length; i++) { if (S.queue[i].cropEl) { first = S.queue[i].cropEl; break; } }
  if (!first) return;
  var cw = first.clientWidth;
  var ch = first.clientHeight;
  if (!cw || !ch) return;
  var hasImg = !!(S.imageLoaded && S.pageImageUrl && S.coordW && S.pageH);
  S.queue.forEach(function (w) {
    var c = w.cropEl;
    if (!c) return;
    c.innerHTML = "";
    if (!hasImg) {
      c.classList.add("is-placeholder");
      c.style.backgroundImage = "";
      var ph = document.createElement("span");
      ph.className = "qcrop-word";
      ph.textContent = w.orig;
      c.appendChild(ph);
      return;
    }
    c.classList.remove("is-placeholder");
    var r = cropRect(w, cw, ch);
    c.style.backgroundImage = "url(\"" + S.pageImageUrl + "\")";
    c.style.backgroundSize = (S.coordW * r.k).toFixed(2) + "px " + (S.pageH * r.k).toFixed(2) + "px";
    c.style.backgroundPosition = (-r.x * r.k).toFixed(2) + "px " + (-r.y * r.k).toFixed(2) + "px";
    var m = document.createElement("span");
    m.className = "qcrop-mark";
    m.style.left = ((w.bbox[0] - r.x) * r.k).toFixed(1) + "px";
    m.style.top = ((w.bbox[1] - r.y) * r.k).toFixed(1) + "px";
    m.style.width = (w.bbox[2] * r.k).toFixed(1) + "px";
    m.style.height = (w.bbox[3] * r.k).toFixed(1) + "px";
    c.appendChild(m);
  });
}

/* Crop window in bbox space for a cw x ch px crop box: {x, y, k} where k is
   crop px per bbox px. Kept inside the page where the page is big enough. */
function cropRect(w, cw, ch) {
  var lb = w.line.bbox, wb = w.bbox;
  var aspect = cw / ch;
  var rh = Math.max(lb[3], wb[3], 1) * (1 + 2 * CROP_MARGIN);
  var rw = rh * aspect;
  if (rw < wb[2] * 1.15) { rw = wb[2] * 1.15; rh = rw / aspect; }
  var x = wb[0] + wb[2] / 2 - rw / 2;
  var y = lb[1] + lb[3] / 2 - rh / 2;
  x = Math.max(0, Math.min(x, S.coordW - rw)); if (S.coordW < rw) x = (S.coordW - rw) / 2;
  y = Math.max(0, Math.min(y, S.pageH - rh)); if (S.pageH < rh) y = (S.pageH - rh) / 2;
  return { x: x, y: y, k: cw / rw };
}

function renderCounts() {
  var total = S.queue.length;
  var fixed = fixedCount();
  var left = total - fixed;
  el.leftPill.textContent = left + " left";
  var r = routingSummary();
  el.routeCount.innerHTML = "";
  var n1 = document.createElement("b");
  n1.textContent = String(r.auto);
  var n2 = document.createElement("b");
  n2.textContent = String(r.routed);
  el.routeCount.appendChild(n1);
  el.routeCount.appendChild(document.createTextNode(" of " + r.total + " lines auto-accepted · "));
  el.routeCount.appendChild(n2);
  el.routeCount.appendChild(document.createTextNode(r.routed === 1 ? " needs you" : " need you"));
  el.fixedCount.textContent = fixed + "/" + total + " fixed";
  var scope = S.mode === "auto" ? "Doubt words only (Auto)" : "Doubt + OK words (Review)";
  el.railSub.textContent = scope + (S.dictCount ? " · " + S.dictCount + " auto-applied" : "");
  var pct = total ? Math.round(fixed / total * 100) : 100;
  el.progressFill.style.width = pct + "%";
  el.progressBar.setAttribute("aria-valuenow", String(pct));
  if (el.snReviewCount) el.snReviewCount.textContent = left ? String(left) : "";
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
  var STATE_LABEL = { repaired: "Repaired", clean: "Clean", precomputed: "Precomputed", ready: "Ready", queued: "Queued" };
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
  /* Export is always reachable once a page is loaded, whatever the page
     state (was: only clean/repaired/precomputed, which left Sarvam and stub
     pages - state "queued" - with a dead button). The export dialog handles
     unsaved / local-only / failed-save cases. */
  el.exportBtn.disabled = false;
  renderPager();
}

/* Page switcher: one page per load (editor.html?job=..&page=N), so a page's
   text, words and queue are built only from that page. Saves pending fixes
   before leaving. Live multi-page jobs only. */
function gotoPage(n) {
  if (!S.jobId || n < 1 || n > S.pageCount || n === Number(S.page.page)) return;
  var q = new URLSearchParams(location.search);
  q.set("job", S.jobId);
  q.set("page", String(n));
  var go = function () { location.href = "./editor.html?" + q.toString(); };
  flushSave().then(go, go);
}

function renderPager() {
  var old = el.pageBadges.querySelector(".page-pager");
  if (old) old.remove();
  if (!S.jobId || !(S.pageCount > 1)) return;
  var cur = Number(S.page.page);
  var wrap = document.createElement("span");
  wrap.className = "page-pager";
  [["prev", -1, "Previous page", "M10 3.5 5.5 8l4.5 4.5"], ["next", 1, "Next page", "M6 3.5 10.5 8 6 12.5"]].forEach(function (d) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn--soft btn--sm btn--icon";
    b.setAttribute("aria-label", d[2] + " (" + (cur + d[1]) + " of " + S.pageCount + ")");
    b.title = d[2];
    b.disabled = cur + d[1] < 1 || cur + d[1] > S.pageCount;
    b.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + d[3] + '"/></svg>';
    b.addEventListener("click", function () { gotoPage(cur + d[1]); });
    wrap.appendChild(b);
  });
  el.pageBadges.appendChild(wrap);
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
  if (w.sugg.length) {
    var plist = document.createElement("div");
    plist.className = "qsugg pop-sugg";
    plist.setAttribute("role", "group");
    plist.setAttribute("aria-label", "Suggestions - press 1 to " + w.sugg.length + " in an empty box");
    w.sugg.forEach(function (c, i) { plist.appendChild(suggButton(w, c, i)); });
    el.pop.appendChild(plist);
  }
  el.pop.appendChild(evidence);
  el.pop.appendChild(actions);

  input.addEventListener("input", function () {
    preview.textContent = window.PC_Translit.transliterate(input.value);
  });
  input.addEventListener("keydown", function (e) {
    /* Digits are not Tanglish: in an empty box 1-9 pick a suggestion. */
    if (!input.value && /^[1-9]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey &&
        parseInt(e.key, 10) <= w.sugg.length) {
      e.preventDefault();
      pickSuggestion(w, parseInt(e.key, 10) - 1);
      return;
    }
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
  acceptFix(w, tamil);
  toast("Fix accepted · " + tamil + " marked human");
  if (w.el) w.el.focus();
}

/* Number key / click on a suggestion: accept candidate i as a human fix,
   then move to the next open item so a keyboard run stays 1 key per word. */
function pickSuggestion(w, i) {
  if (!w) return;
  var c = w.sugg[i];
  if (!c) return;
  acceptFix(w, c.text);
  toast("Fix accepted · " + c.text + " (suggestion " + (i + 1) + ") marked human");
  var open = S.queue.some(function (q) { return !q.fixed; });
  if (open) stepQueue(1);
}

/* Shared accept path: the word becomes a human fix, is saved back, and
   teaches the dictionary. */
function acceptFix(w, tamil) {
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
  /* The auto-fix was recorded as a correction; drop it so the server copy
     (and export) goes back to the OCR text too. */
  if (S.corrections[w.key]) {
    delete S.corrections[w.key];
    S.corrDirty = true;
    scheduleSave();
  }
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
    if (S.exportOpen) return; /* the export dialog owns the keys while open */
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
    if (/^[1-9]$/.test(key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      var aw = S.byKey[S.activeKey];
      if (aw && aw.sugg.length >= parseInt(key, 10)) { e.preventDefault(); pickSuggestion(aw, parseInt(key, 10) - 1); }
    }
    else if (key === "j") { e.preventDefault(); stepQueue(1); }
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

/* ---------------- export dialog ----------------
   Export opens a small dialog. Its main action saves pending corrections
   (flushSave) and only then opens export.html?job=<id> (built separately).
   Corrections that could not reach the server keep the existing handling -
   a corrected-text download built in the browser - and the dialog says
   plainly that the PDF will not include them. */
var exp = {};

function exportPageUrl() {
  var q = "job=" + encodeURIComponent(S.jobId);
  if (window.PC_API.API_BASE) q += "&api=" + encodeURIComponent(window.PC_API.API_BASE);
  return "./export.html?" + q;
}

function fixCount() { return corrPayload().corrections.length; } /* whole job, all pages */

function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

/* Where the fixes stand after a save attempt. */
function exportState() {
  if (window.PC_API.USE_MOCK || !S.jobId) return "demo";
  if (S.saveMode === "local") return "local";            /* route missing: fixes live in this browser only */
  if (S.saveError) return "error";                       /* server refused the last save (500/422) */
  if (S.corrDirty && fixCount() > 0) return "unsynced";  /* backend unreachable: local copy only */
  return "saved";
}

function setExpButton(btn, label, hidden) {
  btn.hidden = !!hidden;
  var span = btn.querySelector(".exp-lb");
  if (span) span.textContent = label; else btn.textContent = label;
}

function renderExport(busy) {
  var n = fixCount();
  var st = busy ? "saving" : exportState();
  exp.st = st;
  exp.pages.textContent = String(S.pageCount || 1);
  exp.fixes.textContent = n ? plural(n, "fix", "fixes") : "None yet";
  var SAVED = {
    saving: "Saving…",
    demo: "Demo - stays in this browser",
    saved: n ? "All on the server" : "Nothing to save",
    local: "This browser only",
    unsynced: "This browser only",
    error: "Not saved on the server"
  };
  exp.saved.textContent = SAVED[st];
  exp.saved.className = "exp-val" + (st === "local" || st === "unsynced" || st === "error" ? " is-warn" : "");
  var note = "";
  if (st === "local" || st === "unsynced") {
    note = plural(n, "fix is", "fixes are") + " saved only in this browser" +
      (st === "unsynced" ? " because the server can't be reached" : "") +
      ". The PDF is built from the server's copy, so it will not include " + (n === 1 ? "it" : "them") +
      ". Download the corrected text to keep " + (n === 1 ? "it." : "them.");
  } else if (st === "error") {
    note = "The server could not save the latest fixes. A PDF made now may not include them.";
  } else if (st === "demo") {
    note = "Demo mode: export runs on a live job.";
  }
  exp.note.textContent = note;
  exp.note.hidden = !note;
  exp.go.disabled = st === "saving";
  if (st === "local" || st === "unsynced") {
    setExpButton(exp.go, "Download text");
    setExpButton(exp.alt, "PDF without fixes", false);
  } else if (st === "error") {
    setExpButton(exp.go, "Retry save");
    setExpButton(exp.alt, "Open export anyway", false);
  } else if (st === "demo") {
    setExpButton(exp.go, "Done");
    setExpButton(exp.alt, "", true);
  } else {
    setExpButton(exp.go, st === "saving" ? "Saving…" : "Open export");
    setExpButton(exp.alt, "", true);
  }
}

function openExport() {
  if (!S.page) {
    /* Live job that didn't load here (still processing, failed, bad page):
       the export page has its own pending / error states. */
    if (JOB_ID && !window.PC_API.USE_MOCK) {
      var q = "job=" + encodeURIComponent(JOB_ID);
      if (window.PC_API.API_BASE) q += "&api=" + encodeURIComponent(window.PC_API.API_BASE);
      window.location.href = "./export.html?" + q;
    }
    return;
  }
  closePopup();
  exp.lastFocus = document.activeElement;
  S.exportOpen = true;
  exp.root.hidden = false;
  exp.root.classList.add("show");
  el.exportBtn.setAttribute("aria-expanded", "true");
  exp.card.focus();
  if (window.PC_API.USE_MOCK || !S.jobId) { renderExport(false); return; }
  /* Save pending fixes first, so the dialog reports the real state. */
  renderExport(true);
  flushSave().then(function () { if (S.exportOpen) renderExport(false); });
}

function closeExport() {
  if (!S.exportOpen) return;
  S.exportOpen = false;
  exp.root.classList.remove("show");
  exp.root.hidden = true;
  el.exportBtn.setAttribute("aria-expanded", "false");
  if (exp.lastFocus && exp.lastFocus.focus) exp.lastFocus.focus();
}

/* Existing local-fallback handling: this page's corrected text as a file. */
function downloadCorrectedText() {
  var blob = new Blob([S.page.text + "\n"], { type: "text/plain;charset=utf-8" });
  var url = URL.createObjectURL(blob);
  var a = document.createElement("a");
  a.href = url;
  a.download = "pink-cloud-" + String(S.jobId || "demo").slice(0, 8) + "-p" + S.page.page + "-corrected.txt";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  toast("Corrected text downloaded · fixes applied in this browser");
}

function goExport() {
  S.exportOpen = false;
  window.location.href = exportPageUrl();
}

function onExportMain() {
  var st = exp.st;
  if (st === "saving") return;
  if (st === "demo") { closeExport(); toast("Export ready · page " + S.page.page); return; }
  if (st === "local" || st === "unsynced") { downloadCorrectedText(); return; }
  if (st === "error") {
    renderExport(true);
    S.corrDirty = true;
    flushSave().then(function () { if (S.exportOpen) renderExport(false); });
    return;
  }
  /* saved: the server holds every fix - save once more (no-op if clean), then go. */
  renderExport(true);
  flushSave().then(function () {
    if (exportState() === "saved") goExport();
    else if (S.exportOpen) renderExport(false);
  });
}

function wireExport() {
  exp.root = $("exportModal");
  if (!exp.root) return;
  exp.card = exp.root.querySelector(".kb-card");
  exp.pages = $("expPages");
  exp.fixes = $("expFixes");
  exp.saved = $("expSaved");
  exp.note = $("expNote");
  exp.go = $("expGo");
  exp.alt = $("expAlt");
  el.exportBtn.setAttribute("aria-haspopup", "dialog");
  el.exportBtn.setAttribute("aria-expanded", "false");
  exp.go.addEventListener("click", onExportMain);
  exp.alt.addEventListener("click", function () { if (exp.st !== "demo") goExport(); });
  $("expCancel").addEventListener("click", closeExport);
  exp.root.addEventListener("click", function (e) { if (!exp.card.contains(e.target)) closeExport(); });
  /* Capture phase: while open, the dialog owns the keyboard (Esc closes,
     Enter runs the main action, Tab stays inside, editor hotkeys wait). */
  window.addEventListener("keydown", function (e) {
    if (!S.exportOpen) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeExport(); return; }
    if (e.key === "Tab") {
      var f = Array.prototype.filter.call(exp.card.querySelectorAll("button"), function (b) { return !b.hidden && !b.disabled; });
      if (!f.length) return;
      var i = f.indexOf(document.activeElement);
      e.preventDefault();
      var n = i === -1 ? (e.shiftKey ? f.length - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + f.length) % f.length;
      f[n].focus();
      return;
    }
    if (e.key === "Enter") {
      var onBtn = document.activeElement && document.activeElement.tagName === "BUTTON" && exp.card.contains(document.activeElement);
      if (!onBtn) { e.preventDefault(); onExportMain(); }
      e.stopImmediatePropagation();
      return;
    }
    e.stopImmediatePropagation();
  }, true);
}

/* ---------------- sidebar shell ---------------- */
function wireSidenav() {
  var snExport = $("snExport");
  if (snExport) snExport.addEventListener("click", openExport);
  var snUpload = $("snUpload");
  if (snUpload && window.PC_API.USE_MOCK) snUpload.setAttribute("href", "./index.html?mock=1");
  /* Documents -> library.html. It is a plain link: library.html ships with
     the UI, and the old HEAD probe failed on the deploy (the FastAPI UI
     routes are GET-only and answer HEAD with 405), which stripped the href
     and left the button dead. */
  var lib = $("snLibrary");
  if (lib && window.PC_API.USE_MOCK) lib.setAttribute("href", "./library.html?mock=1");
}

/* MOCK suggestions for the offline Kural demo, in the proposed contract
   shape (schema/suggestions-contract.md). The real source fills
   page.suggestions[]; until then the demo shows what the queue does with it. */
var MOCK_SUGGESTIONS = [
  { line: "L3", word: 4, before: "வாழறிவன்", candidates: [
    { text: "வாலறிவன்", score: 0.93, source: "lexicon" },
    { text: "வாளறிவன்", score: 0.41, source: "lexicon" }
  ] }
];

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
  AUTO_MIN = cssNum("--pc-conf-auto-min", 0.95);
  OK_MIN = cssNum("--pc-conf-ok-min", 0.80);
  REVIEW_FLOOR = cssNum("--pc-conf-review-floor", 0.95);
  HEAT_CLEAN = cssNum("--pc-heat-clean", 0.98);
  HEAT_BAD = cssNum("--pc-heat-bad", 0.60);
  MOTION_BASE = cssNum("--pc-motion-base", 160);
  TOAST_MS = cssNum("--pc-toast-duration", 2400);

  el.pageBadges = $("pageBadges");
  el.exportBtn = $("exportBtn");
  /* Live job: Export works from the start, even before (or without) a loaded page. */
  if (JOB_ID && !window.PC_API.USE_MOCK) el.exportBtn.disabled = false;
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
  el.routeCount = $("routeCount");
  el.fixedCount = $("fixedCount");
  el.snReviewCount = $("snReviewCount");

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

  el.exportBtn.addEventListener("click", openExport);
  wireExport();
  wireSidenav();

  wireHotkeys();
  probeConn();

  var raf = 0;
  /* The paper can change width without a window resize (the scan pane's
     scrollbar appearing, the heat legend toggling): keep boxes in step. */
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      if (S.page && el.paper.clientWidth && el.paper.clientWidth !== S.renderedW) {
        renderScan();
      }
    }).observe(el.paper);
  }
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
  S.pageImageUrl = meta.imageUrl || ((meta.jobId && window.PC_API.pageImageUrl)
    ? window.PC_API.pageImageUrl(meta.jobId, doc.page)
    : null);
  S.mockId = meta.mockId || null;
  S.filename = meta.filename || "";
  S.imageTried = false;
  S.imageLoaded = false;
  S.imageFailed = false;
  S.coordW = null;
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
    loadJob(pages[idx], { jobId: JOB_ID, pageCount: pages.length, filename: job.filename || "" });
  }).catch(showFatal);
}

function bootData() {
  if (window.PC_API.USE_MOCK) {
    var fixture = BOOT_QS.get("fixture");
    if (fixture && /^[a-z0-9_-]+$/i.test(fixture)) {
      /* Mock fixture from real Sarvam sample output (samples/editor/, built
         by samples/editor/make_fixtures.py) drawn on its real scan
         (raw/<name>.png) - exercises the queue crops offline. */
      fetch("./samples/editor/" + fixture + ".json", { cache: "no-store" })
        .then(function (res) { if (!res.ok) throw new Error("fixture " + fixture + " (" + res.status + ")"); return res.json(); })
        .then(function (doc) { loadJob(doc, { imageUrl: "./raw/" + fixture + ".png", mockId: "demo-" + fixture }); })
        .catch(function (err) { toast("Could not load fixture · " + err.message); });
      return;
    }
    /* Demo path: the Kural page from schema/doc_demo.json, plus MOCK
       suggestions in the proposed contract shape (the real lexical source is
       not built yet). */
    window.PC_API.getPage(1)
      .then(function (doc) {
        if (!Array.isArray(doc.suggestions)) doc.suggestions = MOCK_SUGGESTIONS;
        loadJob(doc, {});
      })
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

