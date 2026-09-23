/* Pink Cloud - Library (library.html).
   Lists every job (ID, filename, status, pages, fixed) and searches the
   indexed text; a search hit opens editor.html?job=<id>&page=<n>.

   Data seam, same as upload.js / api.js:
     live (default)  -> PC_API.listJobs  (GET /jobs?limit&offset)
                        PC_API.searchJobs (GET /search?q&limit&offset)
     ?mock=1         -> MOCK_JOBS below, answered in the exact live shapes
     ?api=<origin>   -> backend on another origin (carried into links)

   "Fixed" reads corrections_count from GET /jobs: saved reviewer
   corrections, never automatic OCR repairs. Until the backend sends the
   field the column shows "-".
   Search snippets carry <mark>..</mark> around hits. They are parsed as
   text (never innerHTML) and each mark is widened to whole grapheme
   clusters, because the FTS tokenizer can cut a Tamil cluster (e.g. a
   consonant from its pulli) and a split cluster renders broken. */
(function () {
  "use strict";

  var API = window.PC_API;
  var QS = new URLSearchParams(location.search);
  var MOCK = API.USE_MOCK;
  var DEBOUNCE_MS = 220;

  /* ---------------- mock fixture (?mock=1) ----------------
     Shaped like the backend's own data: pages[].lines[] from result.pages
     and corrections[] from GET /jobs/{id}/corrections. The mock computes
     fixed_count and snippets the way the real route should. */
  var MOCK_JOBS = [
    {
      id: "3f9c2a7e5b1d4c8e9a0f6b2d7e1c4a90", filename: "thirukkural-leaf-04.png", status: "done",
      pages: [
        { page: 1, lines: [
          { id: "L1", body: "அகர முதல எழுத்தெல்லாம் ஆதி" },
          { id: "L2", body: "பகவன் முதற்றே உலகு" },
          { id: "L3", body: "கற்றதனால் ஆய பயனென்கொல் வாழறிவன்" },
          { id: "L4", body: "நற்றாள் தொழாஅர் எனின்" }
        ] }
      ],
      corrections: [
        { page: 1, line: "L3", word: 4, before: "வாழறிவன்", after: "வாலறிவன்" }
      ]
    },
    {
      id: "a81e0c44d2f94b6a8c3e7d5f1b09e6c2", filename: "narrinai-cict-p1-3.pdf", status: "done",
      pages: [
        { page: 1, lines: [
          { id: "L1", body: "நின்ற சொல்லர் நீடுதோறு இனியர்" },
          { id: "L2", body: "என்றும் என்தோள் பிரிபு அறியலரே" }
        ] },
        { page: 2, lines: [
          { id: "L1", body: "தாமரைத் தண்தாது ஊதி மீமிசைச்" },
          { id: "L2", body: "சாந்தின் தொடுத்த தீந்தேன் போலப்" }
        ] },
        { page: 3, lines: [
          { id: "L1", body: "புரைய மன்ற புரையோர் கேண்மை" },
          { id: "L2", body: "நீர் இன்று அமையா உலகம் போலத்" },
          { id: "L3", body: "தம் இன்று அமையா நம் நயந்து அருளி" }
        ] }
      ],
      corrections: [
        { page: 1, line: "L1", word: 3, before: "நீடுதோறு", after: "நீடுதோறு" },
        { page: 2, line: "L2", word: 3, before: "தீந்தேன்", after: "தீம்தேன்" },
        { page: 3, line: "L2", word: 4, before: "உலகம்", after: "உலகம்" },
        /* stale: the OCR word at this spot changed, so it no longer applies */
        { page: 3, line: "L1", word: 2, before: "மண்ற", after: "மன்ற" }
      ]
    },
    {
      id: "c07d51b3e8aa4f02b6d99e41f3a2c718", filename: "paripuranam-rmrl028-leaf4.tiff", status: "done",
      pages: [
        { page: 1, lines: [
          { id: "L1", body: "சீர்மலி தில்லைச் சிற்றம் பலத்தே" },
          { id: "L2", body: "ஆடல் புரிந்த அண்ணல் திருவடி" }
        ] }
      ],
      corrections: []
    },
    { id: "e5b2f8c1d3a64e7f9b0c2d8a6e4f1b37", filename: "scan-batch-07.pdf", status: "pending", pages: null, corrections: [] },
    { id: "9d4a6c2e1f8b4d3a7c5e0b9f2a6d8c14", filename: "blurred-leaf.jpg", status: "error", pages: null, corrections: [] }
  ];

  function words(body) { return body.split(/\s+/).filter(Boolean); }

  /* Corrections whose `before` still matches the OCR word (export.py rule). */
  function liveCorrections(job) {
    if (!job.pages) return [];
    return job.corrections.filter(function (c) {
      var pg = job.pages.filter(function (p) { return p.page === c.page; })[0];
      var ln = pg && pg.lines.filter(function (l) { return l.id === c.line; })[0];
      var w = ln && words(ln.body);
      return !!(w && c.word >= 1 && c.word <= w.length && w[c.word - 1] === c.before);
    });
  }

  function correctedLines(job, page) {
    var fixes = liveCorrections(job).filter(function (c) { return c.page === page.page; });
    return page.lines.map(function (l) {
      var w = words(l.body);
      fixes.forEach(function (c) { if (c.line === l.id) w[c.word - 1] = c.after; });
      return w.join(" ");
    });
  }

  var MOCK_T0 = Date.parse("2026-09-23T10:00:00Z");

  function mockSummary(job, k) {
    var done = job.status === "done";
    return {
      job_id: job.id, filename: job.filename, sha256: "0".repeat(64), status: job.status,
      created_at: new Date(MOCK_T0 - k * 3600e3).toISOString(),
      page_count: done ? job.pages.length : null,
      pages_needing_review: done ? 0 : null,
      error: job.status === "error" ? "file could not be decoded" : null,
      result_url: "/jobs/" + job.id,
      receipt_url: done ? "/jobs/" + job.id + "/receipt" : null,
      corrections_count: liveCorrections(job).length
    };
  }

  function mockList(limit, offset) {
    var all = MOCK_JOBS.map(mockSummary);
    return delay({ total: all.length, limit: limit, offset: offset, jobs: all.slice(offset, offset + limit) });
  }

  function mockSearch(q, limit, offset) {
    var needle = q.trim().toLowerCase();
    var results = [];
    MOCK_JOBS.forEach(function (job) {
      if (job.status !== "done") return;
      job.pages.forEach(function (p) {
        correctedLines(job, p).forEach(function (text, i) {
          var at = text.toLowerCase().indexOf(needle);
          if (at < 0) return;
          results.push({
            job_id: job.id, filename: job.filename, page: p.page, line: p.lines[i].id,
            snippet: text.slice(0, at) + "<mark>" + text.slice(at, at + needle.length) + "</mark>" + text.slice(at + needle.length),
            score: -1 - results.length * 0.1
          });
        });
      });
    });
    return delay({ query: q.trim(), total: results.length, limit: limit, offset: offset, results: results.slice(offset, offset + limit) });
  }

  function delay(v) { return new Promise(function (r) { setTimeout(function () { r(v); }, 120); }); }

  var LIST_LIMIT = 50;
  var SEARCH_LIMIT = 20;
  function fetchList(offset) { return MOCK ? mockList(LIST_LIMIT, offset) : API.listJobs(LIST_LIMIT, offset); }
  function fetchSearch(q, offset) { return MOCK ? mockSearch(q, SEARCH_LIMIT, offset) : API.searchJobs(q, SEARCH_LIMIT, offset); }

  /* ---------------- helpers ---------------- */
  function h(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  }

  function editorUrl(id, page) {
    var q = new URLSearchParams();
    if (MOCK) q.set("mock", "1");
    q.set("job", id);
    if (page) q.set("page", String(page));
    if (API.API_BASE) q.set("api", API.API_BASE);
    return "./editor.html?" + q.toString();
  }

  function carryQs(href) {
    var q = new URLSearchParams();
    if (MOCK) q.set("mock", "1");
    if (API.API_BASE) q.set("api", API.API_BASE);
    var s = q.toString();
    return href + (s ? "?" + s : "");
  }

  var STATUS = {
    done: { cls: "pill--success", label: "Done" },
    pending: { cls: "pill--processing", label: "Processing" },
    error: { cls: "pill--error", label: "Failed" }
  };

  function statusPill(status) {
    var s = STATUS[status] || { cls: "pill--default", label: status || "Unknown" };
    var pill = h("span", "pill pill--bar pill--tinted " + s.cls);
    pill.appendChild(h("i", "pill__dot"));
    pill.appendChild(document.createTextNode(s.label));
    return pill;
  }

  function shortId(id) { return String(id || "").slice(0, 8); }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function num(v) { return (typeof v === "number" && isFinite(v)) ? v : null; }

  /* Grapheme cluster boundaries of text (UTF-16 offsets). */
  var SEG = (window.Intl && Intl.Segmenter) ? new Intl.Segmenter("ta", { granularity: "grapheme" }) : null;
  function clusterBounds(text) {
    var set = {};
    set[0] = true; set[text.length] = true;
    if (SEG) {
      var it = SEG.segment(text)[Symbol.iterator](), n;
      while (!(n = it.next()).done) set[n.value.index] = true;
    } else {
      /* fallback: a boundary sits before any char that is not a combining mark */
      for (var i = 1; i < text.length; i++) if (!/\p{M}/u.test(text[i])) set[i] = true;
    }
    return set;
  }

  /* snippet "a <mark>b</mark> c" -> DOM: text + <mark> nodes only. Anything
     else in the string, tags included, stays literal text. */
  function renderSnippet(raw) {
    var OPEN = "<mark>", CLOSE = "</mark>";
    var text = "", ranges = [], i = 0, start = -1;
    while (i < raw.length) {
      if (start < 0 && raw.startsWith(OPEN, i)) { start = text.length; i += OPEN.length; continue; }
      if (start >= 0 && raw.startsWith(CLOSE, i)) {
        if (text.length > start) ranges.push([start, text.length]);
        start = -1; i += CLOSE.length; continue;
      }
      text += raw[i]; i++;
    }
    if (start >= 0 && text.length > start) ranges.push([start, text.length]); /* unclosed mark */

    var ok = clusterBounds(text);
    var merged = [];
    ranges.forEach(function (r) {
      var s = r[0], e = r[1];
      while (s > 0 && !ok[s]) s--;
      while (e < text.length && !ok[e]) e++;
      var last = merged[merged.length - 1];
      if (last && s <= last[1]) last[1] = Math.max(last[1], e); else merged.push([s, e]);
    });

    var frag = document.createDocumentFragment(), pos = 0;
    merged.forEach(function (r) {
      if (r[0] > pos) frag.appendChild(document.createTextNode(text.slice(pos, r[0])));
      frag.appendChild(h("mark", null, text.slice(r[0], r[1])));
      pos = r[1];
    });
    if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
    return frag;
  }

  /* ---------------- render ---------------- */
  var el = {
    list: document.getElementById("libList"),
    count: document.getElementById("libCount"),
    notice: document.getElementById("libNotice"),
    search: document.getElementById("libSearch")
  };
  document.getElementById("navUpload").href = carryQs("./index.html");

  function setNotice(kind, parts) {
    el.notice.textContent = "";
    if (!kind) { el.notice.hidden = true; return; }
    el.notice.className = "lib-notice" + (kind === "error" ? " is-error" : "");
    parts.forEach(function (p) {
      if (typeof p === "string") el.notice.appendChild(document.createTextNode(p));
      else el.notice.appendChild(p);
    });
    el.notice.hidden = false;
  }

  function empty(message, withUpload) {
    var box = h("div", "lib-empty");
    box.appendChild(h("p", null, message));
    if (withUpload) {
      var a = h("a", "btn btn--outline btn--sm", "Upload scans");
      a.href = carryQs("./index.html");
      box.appendChild(a);
    }
    return box;
  }

  function moreButton(label, onClick) {
    var wrap = h("div", "lib-more");
    var b = h("button", "btn btn--outline btn--sm", label);
    b.type = "button";
    b.addEventListener("click", function () { b.disabled = true; onClick(); });
    wrap.appendChild(b);
    return wrap;
  }

  /* ---------- job table ---------- */
  var listState = { jobs: [], total: 0 };

  function jobRow(job) {
    var open = job.status === "done";
    var tr = h("tr", "lib-row" + (open ? "" : " is-off"));
    var id = h("td", "lib-id code", shortId(job.job_id));
    id.title = job.job_id;
    tr.appendChild(id);

    var file = h("td", "lib-file");
    file.title = job.filename || "";
    if (open) {
      var a = h("a", null, job.filename || "(no name)");
      a.href = editorUrl(job.job_id, 1);
      file.appendChild(a);
      tr.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;
        location.href = a.href;
      });
    } else {
      file.textContent = job.filename || "(no name)";
    }
    tr.appendChild(file);

    var st = h("td", "lib-status");
    st.appendChild(statusPill(job.status));
    if (job.status === "error" && job.error) st.title = job.error;
    tr.appendChild(st);

    var pages = num(job.page_count);
    tr.appendChild(h("td", "is-num" + (pages === null ? " lib-muted" : ""), pages === null ? "-" : String(pages)));
    var fixed = open ? num(job.corrections_count) : null;
    var fx = h("td", "is-num " + (fixed ? "lib-fixed" : "lib-muted"), fixed === null ? "-" : String(fixed));
    if (fixed === null) fx.title = open ? "Not reported by this backend yet" : "";
    tr.appendChild(fx);
    return tr;
  }

  function renderTable() {
    var items = listState.jobs;
    el.list.textContent = "";
    el.count.textContent = plural(listState.total, "job", "jobs");
    if (!items.length) { el.list.appendChild(empty("No jobs yet. Upload a scan to start.", true)); return; }

    var table = h("table", "lib-table");
    var head = h("tr");
    [["ID", ""], ["Filename", ""], ["Status", ""], ["Pages", "is-num"], ["Fixed", "is-num"]].forEach(function (c) {
      var th = h("th", c[1] || null, c[0]);
      th.scope = "col";
      if (c[0] === "Fixed") th.title = "Saved reviewer corrections";
      head.appendChild(th);
    });
    var thead = h("thead"); thead.appendChild(head); table.appendChild(thead);
    var tbody = h("tbody");
    items.forEach(function (job) { tbody.appendChild(jobRow(job)); });
    table.appendChild(tbody);
    el.list.appendChild(table);
    if (items.length < listState.total) {
      el.list.appendChild(moreButton("Show more", function () { loadList(items.length); }));
    }
  }

  /* ---------- search results (grouped by job, best match first) ---------- */
  var searchState = { q: "", results: [], total: 0 };

  function renderResults() {
    var q = searchState.q, results = searchState.results;
    el.list.textContent = "";
    if (!results.length) {
      el.count.textContent = "No matches";
      el.list.appendChild(empty("Nothing matches \"" + q + "\".", false));
      return;
    }
    var groups = [], byJob = {};
    results.forEach(function (r) {
      var g = byJob[r.job_id];
      if (!g) { g = byJob[r.job_id] = { job_id: r.job_id, filename: r.filename, hits: [] }; groups.push(g); }
      g.hits.push(r);
    });
    el.count.textContent = plural(searchState.total, "match", "matches") +
      (results.length < searchState.total ? " · showing " + results.length : "");

    groups.forEach(function (g) {
      var group = h("div", "lib-group");
      var gh = h("div", "lib-group-head");
      var idc = h("span", "lib-id code", shortId(g.job_id)); idc.title = g.job_id;
      gh.appendChild(idc);
      var fn = h("span", "lib-file", g.filename || "(no name)"); fn.title = g.filename || "";
      gh.appendChild(fn);
      gh.appendChild(h("span", "spacer"));
      gh.appendChild(h("span", "small lib-muted", plural(g.hits.length, "line", "lines")));
      group.appendChild(gh);

      g.hits.forEach(function (r) {
        var a = h("a", "lib-hit");
        a.href = editorUrl(r.job_id, r.page);
        var pg = h("span", "small lib-hit-page", "Page " + r.page);
        pg.title = "Page " + r.page + ", line " + r.line;
        a.appendChild(pg);
        var t = h("span", "tamil-row lib-hit-text");
        t.appendChild(renderSnippet(String(r.snippet || "")));
        a.appendChild(t);
        group.appendChild(a);
      });
      el.list.appendChild(group);
    });
    if (results.length < searchState.total) {
      el.list.appendChild(moreButton("Show more matches", function () { loadSearch(q, results.length); }));
    }
  }

  function renderFailure(err, searching) {
    el.list.textContent = "";
    el.list.hidden = true;
    el.count.textContent = "";
    var demo = h("a", null, "library.html?mock=1");
    demo.href = "./library.html?mock=1";
    if (err && err.kind === "missing") {
      setNotice("warn", ["This backend has no " + (searching ? "search" : "job list") + " route yet. Update it, or open ", demo, " to preview with sample jobs."]);
    } else if (err && err.kind === "down") {
      setNotice("error", ["Backend unreachable. Start it (fastapi/sqlite/RUN.md) or open ", demo, " for sample jobs."]);
    } else if (err && err.status === 503) {
      setNotice("warn", ["Search is unavailable on this backend (no full-text index in this build). The job list still works."]);
    } else {
      setNotice("error", [(searching ? "Search failed" : "Could not load jobs") + (err && err.message ? ": " + err.message : "") + "."]);
    }
  }

  /* ---------------- load + search ---------------- */
  var seq = 0;
  var timer = null;

  function loadList(offset) {
    var mine = ++seq;
    fetchList(offset).then(function (body) {
      if (mine !== seq) return; /* a newer request won */
      setNotice(null);
      el.list.hidden = false;
      var jobs = (body && body.jobs) || [];
      listState.jobs = offset ? listState.jobs.concat(jobs) : jobs;
      listState.total = num(body && body.total) === null ? listState.jobs.length : body.total;
      renderTable();
    }).catch(function (err) { if (mine === seq) renderFailure(err, false); });
  }

  function loadSearch(q, offset) {
    var mine = ++seq;
    if (!offset) el.count.textContent = "Searching…";
    fetchSearch(q, offset).then(function (body) {
      if (mine !== seq) return;
      setNotice(null);
      el.list.hidden = false;
      var results = (body && body.results) || [];
      searchState.q = q;
      searchState.results = offset ? searchState.results.concat(results) : results;
      searchState.total = num(body && body.total) === null ? searchState.results.length : body.total;
      renderResults();
    }).catch(function (err) { if (mine === seq) renderFailure(err, true); });
  }

  function run(q) {
    q = q.trim().slice(0, 200); /* backend accepts 1..200 chars */
    if (q) loadSearch(q, 0); else loadList(0);
  }

  function syncUrl(q) {
    var p = new URLSearchParams(location.search);
    if (q) p.set("q", q); else p.delete("q");
    var s = p.toString();
    history.replaceState(null, "", location.pathname + (s ? "?" + s : ""));
  }

  el.search.maxLength = 200;
  el.search.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var q = el.search.value.trim();
      syncUrl(q);
      run(q);
    }, DEBOUNCE_MS);
  });

  el.search.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && el.search.value) {
      el.search.value = "";
      clearTimeout(timer);
      syncUrl("");
      run("");
    } else if (e.key === "Enter") {
      var first = el.list.querySelector(".lib-hit");
      if (first && el.search.value.trim()) location.href = first.href;
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== el.search && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      el.search.focus();
    }
  });

  var initial = (QS.get("q") || "").trim();
  el.search.value = initial;
  run(initial);
})();
