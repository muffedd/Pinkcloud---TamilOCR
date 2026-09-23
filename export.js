/* Pink Cloud - Export / Result page (export.html?job=<job_id>).
   GET /jobs/{id}/receipt -> receipt card; downloads link the existing
   GET /jobs/{id}/export.pdf and /export.txt endpoints.
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
