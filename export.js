/* Pink Cloud - Export / Result page (export.html?job=<job_id>).
   GET /jobs/{id}/receipt -> receipt card; downloads link the existing
   GET /jobs/{id}/export.pdf, /export.txt and /export.docx endpoints.
   Markdown, CSV and XML are built in the browser from GET /jobs/{id} plus
   GET /jobs/{id}/corrections, applying the saved fixes with the same rule
   as the server's export.apply_corrections (page, line id, 1-based word;
   stale 'before' skipped), so every format carries the same text.
   404 -> missing job; 409 -> not ready (polls GET /jobs/{id} while pending).
   Same ?api= override as api.js (default: same origin). ?reviewer= is
   passed through to the receipt and both exports. */
(function () {
  "use strict";

  var QS = new URLSearchParams(location.search);
  var API_BASE = (QS.get("api") || "").replace(/\/+$/, "");
  var JOB = (QS.get("job") || "").trim();
  var REVIEWER = (QS.get("reviewer") || "").trim();
  var POLL_MS = 2000;

  var root = document.getElementById("exRoot");
  function $(id) { return document.getElementById(id); }

  function jobUrl(suffix) {
    var u = API_BASE + "/jobs/" + encodeURIComponent(JOB) + (suffix || "");
    return REVIEWER ? u + "?reviewer=" + encodeURIComponent(REVIEWER) : u;
  }

  function show(state) {
    var secs = root.querySelectorAll(".ex-state");
    for (var i = 0; i < secs.length; i++) secs[i].hidden = secs[i].getAttribute("data-state") !== state;
    root.setAttribute("aria-busy", state === "loading" ? "true" : "false");
  }

  /* nav: keep the job in the Review/Export links */
  if (JOB) {
    $("navReview").href = "./editor.html?job=" + encodeURIComponent(JOB);
    $("navExport").href = "./export.html?job=" + encodeURIComponent(JOB);
  } else {
    $("navReview").setAttribute("aria-disabled", "true");
    $("navReview").removeAttribute("href");
  }

  function fmtMs(ms) {
    ms = Number(ms) || 0;
    if (ms < 1000) return ms + " ms";
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + " s";
    var m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + ":" + (r < 10 ? "0" : "") + r + " min";
  }
  function fmtTime(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function kv(label, value, note) {
    var wrap = document.createElement("div");
    var dt = document.createElement("dt"); dt.textContent = label;
    var dd = document.createElement("dd"); dd.textContent = value;
    if (note) {
      var s = document.createElement("span"); s.className = "ex-kv-note"; s.textContent = note;
      dd.appendChild(s);
    }
    wrap.appendChild(dt); wrap.appendChild(dd);
    return wrap;
  }

  function setEngine(engine) {
    if (!engine) return;
    var b = $("engineBadge");
    b.hidden = false;
    b.setAttribute("data-state", engine === "stub" ? "stub" : "ok");
    $("engineText").textContent = engine === "stub" ? "OCR engine: stub" : "OCR engine: " + engine;
  }

  function renderReceipt(r) {
    var pages = r.page_count || 0;
    document.title = "Pink Cloud · Export · " + (r.filename || "job");
    $("rFilename").textContent = r.filename || "Untitled scan";
    $("rSub").textContent = plural(pages, "page", "pages") + " · created " + fmtTime(r.time && r.time.created_at);
    $("rJob").textContent = "Job " + r.job_id;

    $("dlPdf").href = jobUrl("/export.pdf");
    $("dlTxt").href = jobUrl("/export.txt");
    $("dlDocx").href = jobUrl("/export.docx");
    RECEIPT = r;

    var c = r.corrections || {}, p = r.pages || {}, l = r.lines || {}, t = r.time || {}, o = r.ocr || {};
    var tiers = Object.keys(c.by_tier || {}).sort().map(function (k) { return k + " " + c.by_tier[k]; }).join(" · ");
    var kvEl = $("rKv");
    kvEl.textContent = "";
    kvEl.appendChild(kv("Pages", String(pages), "auto " + (p.auto || 0) + " · review " + (p.human_review || 0)));
    kvEl.appendChild(kv("Lines", String(l.total || 0), "auto " + (l.auto || 0) + " · review " + (l.human_review || 0)));
    kvEl.appendChild(kv("Corrections", String(c.total || 0), (tiers || "no tiers") + " · " + plural(c.human_verdicts || 0, "verdict", "verdicts")));
    kvEl.appendChild(kv("Reviewer", r.reviewer || "None recorded"));
    kvEl.appendChild(kv("Processing time", fmtMs(t.processing_ms_total), "exported " + fmtTime(t.exported_at)));
    kvEl.appendChild(kv("OCR engine", o.engine_now || "unknown", o.stub_pages ? plural(o.stub_pages, "stub page", "stub pages") : null));
    setEngine(o.engine_now);

    var m = r.master || {};
    $("rSha").textContent = m.sha256 || "-";
    var v = $("rVerified");
    v.className = "small " + (m.verified_on_disk ? "ex-verified-ok" : "ex-verified-bad");
    v.textContent = m.verified_on_disk ? "Verified on disk" : "Not verified on disk";
    show("ready");
  }

  function renderMissing(msg) {
    if (msg) $("missingText").textContent = msg;
    show("missing");
  }

  function renderNotReady(job) {
    var status = (job && job.status) || "pending";
    var isErr = status === "error";
    var sec = root.querySelector('[data-state="notready"]');
    sec.classList.toggle("is-error", isErr);
    $("nrTitle").textContent = (job && job.filename) || "Job " + JOB;
    $("nrPill").className = "pill pill--bar pill--tinted " + (isErr ? "pill--error" : "pill--processing");
    $("nrPillText").textContent = isErr ? "OCR failed" : "Processing";
    var icon = $("nrIcon");
    icon.classList.toggle("spin", !isErr);
    icon.innerHTML = isErr
      ? '<circle cx="8" cy="8" r="6"/><path d="M8 5v3.5M8 11h.01"/>'
      : '<path d="M8 2a6 6 0 1 1-6 6"/>';
    $("nrHeading").textContent = isErr ? "This job can't be exported" : "Not ready to export yet";
    $("nrText").textContent = isErr
      ? "OCR failed for this scan" + (job && job.error ? " (" + job.error + ")" : "") + ". Upload it again to retry."
      : "OCR is still running. This page updates when the job finishes.";
    show("notready");
    if (!isErr) setTimeout(pollJob, POLL_MS);
  }

  function pollJob() {
    fetch(API_BASE + "/jobs/" + encodeURIComponent(JOB), { cache: "no-store" })
      .then(function (res) {
        if (res.status === 404) return renderMissing();
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json().then(function (job) {
          if (job.status === "done") return loadReceipt();
          renderNotReady(job);
        });
      })
      .catch(function () { setTimeout(pollJob, POLL_MS * 2); });
  }

  function loadReceipt() {
    return fetch(jobUrl("/receipt"), { cache: "no-store" })
      .then(function (res) {
        if (res.status === 404) return renderMissing();
        if (res.status === 409) return pollJob();
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json().then(renderReceipt);
      })
      .catch(function () {
        renderMissing("Could not reach the Pink Cloud backend. Check that it is running, then reload this page.");
        $("missingText").previousElementSibling.textContent = "Receipt unavailable";
      });
  }

  /* ---------- client-built formats: Markdown, CSV, XML ---------- */
  var RECEIPT = null;

  function getJson(url, okMissing) {
    return fetch(url, { cache: "no-store" }).then(function (res) {
      if (okMissing && res.status === 404) return null;
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    });
  }

  /* Mirror of fastapi/sqlite/app/export.py apply_corrections. */
  function correctedPages(pages, corrections) {
    var by = {};
    (corrections || []).forEach(function (c) {
      var page = parseInt(c.page, 10), word = parseInt(c.word, 10);
      var after = String(c.after == null ? "" : c.after).trim();
      if (!(page >= 1) || !(word >= 1) || !after) return;
      by[page + "|" + c.line + "|" + word] = { before: c.before == null ? null : String(c.before), after: after };
    });
    return (pages || []).map(function (p, i) {
      var pno = parseInt(p.page, 10) || i + 1;
      var lines = (p.lines || []).slice().sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); }).map(function (l) {
        var words = String(l.body || "").split(/\s+/).filter(Boolean);
        for (var w = 1; w <= words.length; w++) {
          var c = by[pno + "|" + l.id + "|" + w];
          if (c && (c.before === null || c.before === words[w - 1])) words[w - 1] = c.after;
        }
        return { id: l.id, seq: l.seq, confidence: l.confidence, text: words.join(" ") };
      });
      return { page: pno, profile: p.profile, lines: lines, text: lines.length ? null : String(p.text || "") };
    });
  }

  function pageLines(p) {
    return p.lines.length ? p.lines.map(function (l) { return l.text; }) : (p.text ? p.text.split("\n") : []);
  }

  function mdEsc(t) { return t.replace(/([\\`*_\[\]<>|])/g, "\\$1").replace(/^(\s*)([#>+-]|\d+[.)])/, "$1\\$2"); }
  function buildMd(pages, r) {
    var out = ["# " + mdEsc(r.filename || "Pink Cloud export"), "",
      "> Pink Cloud export · job " + r.job_id + " · master SHA-256 " + ((r.master || {}).sha256 || "-"), ""];
    pages.forEach(function (p) {
      out.push("## Page " + p.page, "");
      var ls = pageLines(p).map(mdEsc);
      out.push(ls.length ? ls.join("  \n") : "_(no text)_", "");
    });
    return out.join("\n");
  }

  function csvCell(v) {
    v = v == null ? "" : String(v);
    return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }
  function buildCsv(pages) {
    var rows = [["page", "line", "seq", "text", "confidence"]];
    pages.forEach(function (p) {
      if (p.lines.length) p.lines.forEach(function (l) { rows.push([p.page, l.id, l.seq, l.text, l.confidence]); });
      else pageLines(p).forEach(function (t, i) { rows.push([p.page, "", i + 1, t, ""]); });
    });
    /* BOM so Excel opens the Tamil text as UTF-8 */
    return "\ufeff" + rows.map(function (r) { return r.map(csvCell).join(","); }).join("\r\n") + "\r\n";
  }

  function xmlEsc(v) {
    return String(v == null ? "" : v)
      .replace(/[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\uD800-\uDFFF]/g, "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function buildXml(pages, r) {
    var m = r.master || {};
    var out = ['<?xml version="1.0" encoding="UTF-8"?>',
      '<document generator="Pink Cloud" job="' + xmlEsc(r.job_id) + '" filename="' + xmlEsc(r.filename) +
      '" master-sha256="' + xmlEsc(m.sha256) + '" pages="' + pages.length + '">'];
    pages.forEach(function (p) {
      out.push('  <page n="' + p.page + '"' + (p.profile ? ' profile="' + xmlEsc(p.profile) + '"' : "") + ">");
      if (p.lines.length) p.lines.forEach(function (l) {
        out.push('    <line id="' + xmlEsc(l.id) + '" seq="' + xmlEsc(l.seq) + '"' +
          (l.confidence != null ? ' confidence="' + xmlEsc(l.confidence) + '"' : "") + ">" + xmlEsc(l.text) + "</line>");
      });
      else pageLines(p).forEach(function (t, i) { out.push('    <line seq="' + (i + 1) + '">' + xmlEsc(t) + "</line>"); });
      out.push("  </page>");
    });
    out.push("</document>", "");
    return out.join("\n");
  }

  var FORMATS = {
    md: { build: buildMd, mime: "text/markdown;charset=utf-8" },
    csv: { build: buildCsv, mime: "text/csv;charset=utf-8" },
    xml: { build: buildXml, mime: "application/xml;charset=utf-8" }
  };

  function saveBlob(text, mime, name) {
    var url = URL.createObjectURL(new Blob([text], { type: mime }));
    var a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function clientExport(fmt, btn) {
    var f = FORMATS[fmt], err = $("moreErr");
    if (!f || !RECEIPT) return;
    err.hidden = true;
    btn.disabled = true;
    Promise.all([
      getJson(API_BASE + "/jobs/" + encodeURIComponent(JOB)),
      getJson(API_BASE + "/jobs/" + encodeURIComponent(JOB) + "/corrections", true)
    ]).then(function (res) {
      var pages = correctedPages(res[0].result && res[0].result.pages, res[1] && res[1].corrections);
      var stem = String(RECEIPT.filename || "pinkcloud").replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]+/g, "_") || "pinkcloud";
      saveBlob(f.build(pages, RECEIPT), f.mime, stem + "." + fmt);
    }).catch(function (e) {
      err.textContent = "Could not build the " + fmt.toUpperCase() + " file (" + e.message + "). Try again, or use TXT.";
      err.hidden = false;
    }).then(function () { btn.disabled = false; });
  }

  ["dlMd", "dlCsv", "dlXml"].forEach(function (id) {
    var b = $(id);
    b.addEventListener("click", function () { clientExport(b.getAttribute("data-fmt"), b); });
  });

  /* copy the full hash */
  $("copySha").addEventListener("click", function () {
    var sha = $("rSha").textContent;
    function done(label) {
      $("copyLbl").textContent = label; $("copyLbl2").textContent = label;
      setTimeout(function () { $("copyLbl").textContent = "Copy"; $("copyLbl2").textContent = "Copy"; }, 1600);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(sha).then(function () { done("Copied"); }, function () { done("Select to copy"); });
    } else {
      var range = document.createRange(); range.selectNodeContents($("rSha"));
      var sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      done("Selected");
    }
  });

  if (!JOB) {
    renderMissing("Open this page from the editor's Export button, or upload a scan first.");
    $("missingText").previousElementSibling.textContent = "No job selected";
  } else {
    show("loading");
    loadReceipt();
  }
})();
