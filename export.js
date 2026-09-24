/* Pink Cloud - Export / Result page (export.html?job=<job_id>).
   GET /jobs/{id} -> header (filename, pages, created); downloads link the existing
   GET /jobs/{id}/export.pdf, /export.txt and /export.docx endpoints.
   Markdown, CSV and XML are built in the browser from GET /jobs/{id} plus
   GET /jobs/{id}/corrections, applying the saved fixes with the same rule
   as the server's export.apply_corrections (page, line id, 1-based word;
   stale 'before' skipped), so every format carries the same text.
   404 -> missing job; 409 -> not ready (polls GET /jobs/{id} while pending).
   Same ?api= override as api.js (default: same origin). Every export
   carries ONLY the transcribed text: no receipt card here and no receipt
   fields in any file (the receipt JSON stays at GET /jobs/{id}/receipt). */
(function () {
  "use strict";

  var QS = new URLSearchParams(location.search);
  var API_BASE = (QS.get("api") || "").replace(/\/+$/, "");
  var JOB = (QS.get("job") || "").trim();
  var POLL_MS = 2000;

  var root = document.getElementById("exRoot");
  function $(id) { return document.getElementById(id); }

  function jobUrl(suffix) {
    return API_BASE + "/jobs/" + encodeURIComponent(JOB) + (suffix || "");
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

  function fmtTime(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function setEngine(engine) {
    if (!engine) return;
    var b = $("engineBadge");
    b.hidden = false;
    b.setAttribute("data-state", engine === "stub" ? "stub" : "ok");
    $("engineText").textContent = engine === "stub" ? "OCR engine: stub" : "OCR engine: " + engine;
  }

  function renderReady(job) {
    var pages = ((job.result && job.result.pages) || []).length;
    document.title = "Pink Cloud · Export · " + (job.filename || "job");
    $("rFilename").textContent = job.filename || "Untitled scan";
    $("rSub").textContent = plural(pages, "page", "pages") + " · created " + fmtTime(job.created_at);
    $("dlPdf").href = jobUrl("/export.pdf");
    $("dlTxt").href = jobUrl("/export.txt");
    $("dlDocx").href = jobUrl("/export.docx");
    JOBINFO = job;
    show("ready");
  }
  /* OCR engine badge from GET /health (same probe the editor uses). */
  function loadEngine() {
    fetch(API_BASE + "/health", { cache: "no-store" })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (h) { if (h) setEngine(h.ocr_engine); })
      .catch(function () { /* badge stays hidden */ });
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
          if (job.status === "done") return renderReady(job);
          renderNotReady(job);
        });
      })
      .catch(function () { setTimeout(pollJob, POLL_MS * 2); });
  }

  function loadJob() {
    return fetch(API_BASE + "/jobs/" + encodeURIComponent(JOB), { cache: "no-store" })
      .then(function (res) {
        if (res.status === 404) return renderMissing();
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json().then(function (job) {
          if (job.status === "done") return renderReady(job);
          renderNotReady(job);
        });
      })
      .catch(function () {
        renderMissing("Could not reach the Pink Cloud backend. Check that it is running, then reload this page.");
        $("missingText").previousElementSibling.textContent = "Export unavailable";
      });
  }

  /* PDF download: fetch it here instead of a bare link, so a slow multi-page
     build shows a visible busy state and a failure shows the server's real
     status/detail instead of a silent or generic browser error. */
  var pdfBusy = false;
  function pdfStatus(msg, isErr) {
    var p = $("pdfStatus");
    p.textContent = msg || "";
    p.hidden = !msg;
    p.style.color = isErr ? "var(--pc-color-error)" : "";
  }
  function pdfLabel(label) {
    var spans = $("dlPdf").querySelectorAll(".lb > span");
    for (var i = 0; i < spans.length; i++) spans[i].textContent = label;
  }
  function pdfFilename(res) {
    var cd = res.headers.get("Content-Disposition") || "";
    var m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    if (m) { try { return decodeURIComponent(m[1]); } catch (e) { /* fall through */ } }
    m = /filename="?([^";]+)"?/i.exec(cd);
    return m ? m[1] : "pinkcloud-" + JOB + ".pdf";
  }
  function pdfError(res) {
    return res.text().then(function (body) {
      var detail = "";
      try {
        var j = JSON.parse(body);
        detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j);
      } catch (e) { detail = (body || "").slice(0, 200); }
      throw new Error("HTTP " + res.status + (detail ? " - " + detail : ""));
    });
  }
  $("dlPdf").addEventListener("click", function (e) {
    e.preventDefault();
    if (pdfBusy) return;
    var btn = $("dlPdf");
    pdfBusy = true;
    btn.setAttribute("aria-busy", "true");
    btn.setAttribute("aria-disabled", "true");
    btn.style.opacity = "0.6";
    btn.style.cursor = "progress";
    pdfLabel("Preparing PDF…");
    pdfStatus("Building the PDF - multi-page scans can take a minute.");
    fetch(btn.href, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) return pdfError(res);
        var name = pdfFilename(res);
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url; a.download = name;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
          pdfStatus("PDF downloaded.");
        });
      })
      .catch(function (err) {
        var msg = err && err.message ? err.message : String(err);
        if (err instanceof TypeError) msg = "Could not reach the backend (" + msg + ")";
        pdfStatus("PDF export failed: " + msg, true);
      })
      .then(function () {
        pdfBusy = false;
        btn.removeAttribute("aria-busy");
        btn.removeAttribute("aria-disabled");
        btn.style.opacity = "";
        btn.style.cursor = "";
        pdfLabel("Download PDF");
      });
  });

  /* ---------- client-built formats: Markdown, CSV, XML ---------- */
  var JOBINFO = null;

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
  /* Transcribed text only: "## Page N" headings on multi-page jobs, no
     title, job id or hash. */
  function buildMd(pages) {
    var multi = pages.length > 1, out = [];
    pages.forEach(function (p) {
      var ls = pageLines(p).map(mdEsc);
      if (multi) out.push("## Page " + p.page, "", ls.length ? ls.join("  \n") : "_(no text)_", "");
      else if (ls.length) out.push(ls.join("  \n"), "");
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
  function buildXml(pages) {
    var out = ['<?xml version="1.0" encoding="UTF-8"?>',
      '<document pages="' + pages.length + '">'];
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
    if (!f || !JOBINFO) return;
    err.hidden = true;
    btn.disabled = true;
    Promise.all([
      getJson(API_BASE + "/jobs/" + encodeURIComponent(JOB)),
      getJson(API_BASE + "/jobs/" + encodeURIComponent(JOB) + "/corrections", true)
    ]).then(function (res) {
      var pages = correctedPages(res[0].result && res[0].result.pages, res[1] && res[1].corrections);
      var stem = String(JOBINFO.filename || "pinkcloud").replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]+/g, "_") || "pinkcloud";
      saveBlob(f.build(pages), f.mime, stem + "." + fmt);
    }).catch(function (e) {
      err.textContent = "Could not build the " + fmt.toUpperCase() + " file (" + e.message + "). Try again, or use TXT.";
      err.hidden = false;
    }).then(function () { btn.disabled = false; });
  }

  ["dlMd", "dlCsv", "dlXml"].forEach(function (id) {
    var b = $(id);
    b.addEventListener("click", function () { clientExport(b.getAttribute("data-fmt"), b); });
  });

  if (!JOB) {
    renderMissing("Open this page from the editor's Export button, or upload a scan first.");
    $("missingText").previousElementSibling.textContent = "No job selected";
  } else {
    show("loading");
    loadEngine();
    loadJob();
  }
})();
