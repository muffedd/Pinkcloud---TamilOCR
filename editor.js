/* Pink Cloud correction editor (page 2 of 3).
   Vanilla JS, fully offline. One selection model drives the scan,
   the text pane and the review queue. */

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
  scale: 1
};

var AUTO_MIN = 0.9;
var OK_MIN = 0.75;
var MOTION_BASE = 160;
var TOAST_MS = 2400;

var el = {};

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
    (w.key === S.activeKey ? " is-active" : "") +
    (w.key === S.hoverKey ? " is-hover" : "");
  if (w.el.textContent !== w.text) w.el.textContent = w.text;
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
      s.addEventListener("click", function () { select(w.key, "word"); });
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
  if (w.bin === "doubt") return "box box-doubt";
  if (w.bin === "auto" && S.mode === "review") return "box box-auto";
  return "box";
}

function renderScan() {
  var paperW = el.paper.clientWidth;
  if (!paperW) return;
  S.scale = paperW / IMG_W;
  var s = S.scale;
  el.paper.style.height = Math.round(PAGE_H * s) + "px";
  el.paper.innerHTML = "";

  /* Placeholder sheet: each line body placed at its bbox. */
  S.lines.forEach(function (line) {
    var d = document.createElement("div");
    d.className = "paper-line";
    d.style.left = Math.round(line.bbox[0] * s) + "px";
    d.style.top = Math.round(line.bbox[1] * s) + "px";
    d.style.width = Math.round(line.bbox[2] * s) + "px";
    d.textContent = line.body;
    el.paper.appendChild(d);
  });

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
    meta.textContent = "p" + w.page + " · " + w.lineId + " · w" + w.idx + (w.prov !== "raw" ? " · " + w.prov : "");
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
  el.railSub.textContent = fixed + " of " + total + " fixed · " + scope;
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
  b.innerHTML = ICONS.wrench + "<span></span>";
  b.querySelector("span").textContent = "P" + S.page.page + " · Repaired · " + doubtLeft + " doubt";
  b.addEventListener("click", function () {
    clearActive();
    el.scanScroll.scrollTop = 0;
    el.textScroll.scrollTop = 0;
    el.queue.scrollTop = 0;
  });
  el.pageBadges.appendChild(b);
  el.scanSub.textContent = "page " + S.page.page + " of 1 · " + S.page.profile + " · " + state;
  var open = state === "clean" || state === "repaired" || state === "precomputed";
  el.exportBtn.disabled = !open;
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
  chip.appendChild(document.createTextNode(w.prov + " · " + (w.tier || "—")));
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
  evidence.textContent = "OCR read " + w.orig + " · " + (w.evidence || "no correction on file");

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
  var maxLeft = Math.max(8, bodyW - 300 - 8);
  var left = w.el.offsetLeft + w.el.offsetWidth / 2 - 150;
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
  ax = Math.max(12, Math.min(300 - 24, ax));
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
  refreshWordEl(w);
  w.line.body = w.line.words.map(function (x) { return x.text; }).join(" ");
  S.page.text = S.lines.map(function (l) { return l.body; }).join("\n");
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

/* ---------------- boot ---------------- */

function loadPage(doc) {
  buildModel(doc);
  renderText();
  renderScan();
  renderLegend();
  renderQueue();
  renderCounts();
  renderBadges();
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
    if (S.page) toast("Export ready · page " + S.page.page);
  });

  wireHotkeys();

  var raf = 0;
  window.addEventListener("resize", function () {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      if (S.page) renderScan();
    });
  });

  window.PC_API.getPage(1).then(loadPage).catch(function (err) {
    toast("Could not load page · " + err.message);
  });
}

document.addEventListener("DOMContentLoaded", init);
})();
