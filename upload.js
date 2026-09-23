/* =========================================================
   Pink Cloud — slice 3: Upload screen (page 1, layout plan zone 2)
   Mount: <div id="upload-root"> in index.html.
   Vanilla JS. No packages, no network beyond the API seam.

   Status flow per page: queued -> uploading -> processing -> done|error
   ========================================================= */
(() => {
  'use strict';

  /* =======================================================
     API SEAM — the only place that knows about the backend.
     Flip MOCK to false when the FastAPI backend serves the
     frontend: that single line is the swap. (fastapi/sqlite:
     POST /jobs takes ONE file and returns {job_id};
     GET /jobs/{job_id} → status pending|done|error with
     result = {pages:[schema.json docs…]} or {error}.)
     ======================================================= */
  const MOCK = true;

  const POST_JOBS = '/jobs';
  const POLL_JOB = (id) => `/jobs/${encodeURIComponent(id)}`;
  const POLL_MS = 400;
  const POLL_MAX = 600; // depth cap ≈ 4 min per job

  const ACCEPT_EXT = ['.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff'];
  const ACCEPT_MIME = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff'];
  const ACCEPT_ATTR = '.pdf,.jpg,.jpeg,.png,.tif,.tiff';

  /* Copy of schema/doc_demo.json — last resort only (no fetch, no
     inline #doc). The live source is schema/doc_demo.json. */
  const DOC_FALLBACK = {
    page: 1, profile: 'HEAVY', preprocessed: true, needs_review: true,
    quality: { blur: 38.4, contrast: 0.27, noise: 0.63, skew_deg: 3.4,
      damage_flags: ['yellowed', 'stained'], ink_density: 0.14,
      estimated_lines: 3, quality_score: 34 },
    lines: [
      { id: 'L1', seq: 1, body: 'அகர முதல எழுத்தெல்லாம் ஆதி', bbox: [180, 320, 1240, 90], confidence: 0.93, kural: 1 },
      { id: 'L2', seq: 2, body: 'பகவன் முதற்றே உலகு', bbox: [200, 470, 1180, 88], confidence: 0.88, kural: 1 },
      { id: 'L3', seq: 3, body: 'கற்றதனால் ஆய பயனென்கொல் வாழறிவன்', bbox: [170, 620, 1270, 92], confidence: 0.41, kural: 2 }
    ],
    corrections: [
      { before: 'வாழறிவன்', after: 'வாலறிவன்', tier: 'T1',
        evidence: 'ழ/ல glyph confusion on a stained, low-contrast stroke: the ல in வால் was misread as the looped ழ, yielding வாழறிவன் instead of வாலறிவன் (Kural 2).' }
    ],
    text: 'அகர முதல எழுத்தெல்லாம் ஆதி பகவன் முதற்றே உலகு கற்றதனால் ஆய பயனென்கொல் வாலறிவன்',
    processing_ms: 4820, work: 'திருக்குறள்', chapter: 1, script: 'Tamil', material: 'paper'
  };

  /* Mock doc source: schema/doc_demo.json → the page's inline #doc JSON
     (index.html embeds the same payload offline) → embedded copy. */
  let docPromise = null;
  function getMockDoc() {
    if (docPromise) return docPromise;
    docPromise = fetch('schema/doc_demo.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .catch(() => {
        const tag = document.getElementById('doc');
        if (tag) { try { return JSON.parse(tag.textContent); } catch (e) { /* fall through */ } }
        return DOC_FALLBACK;
      });
    return docPromise;
  }

  /* -------------------------------------------------------
     Mock engine — one job per file (same shape as the real
     backend). A pure function of elapsed time: deterministic,
     no stray timers.
     ------------------------------------------------------- */
  const UP_WINDOW = 1400;   // uploading window, ms
  const PROC_WINDOW = 1800; // processing window, ms
  let mockSeq = 0;
  const mockJobs = new Map(); // job_id -> { t0, doc, fail }

  function mockStartPage(file, opts) {
    return getMockDoc().then((doc) => {
      const job_id = 'mock-' + (++mockSeq);
      mockJobs.set(job_id, {
        t0: performance.now() + (opts.delay || 0),
        doc,
        // Last page of a multi-file batch arrives truncated (a
        // realistic damaged-scan demo); single pages always succeed.
        fail: opts.fail
      });
      return { job_id };
    });
  }

  function mockPollPage(jobId) {
    const job = mockJobs.get(jobId);
    if (!job) return Promise.reject(new Error('unknown job: ' + jobId));
    const el = performance.now() - job.t0;
    let out;
    if (el < 0) out = { status: 'queued', progress: 0 };
    else if (el < UP_WINDOW) out = { status: 'uploading', progress: Math.max(4, Math.round((el / UP_WINDOW) * 100)) };
    else if (el < UP_WINDOW + PROC_WINDOW) out = { status: 'processing', progress: 100 };
    else if (job.fail) out = { status: 'error', progress: 0, error: 'Unreadable after repair: page truncated in scan' };
    else out = { status: 'done', progress: 100, result: structuredClone(job.doc) };
    if (out.status === 'done' || out.status === 'error') mockJobs.delete(jobId);
    return Promise.resolve(out);
  }

  function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }

  /* -------------------------------------------------------
     Real backend (fastapi/sqlite). POST processes one file
     synchronously, so a poll may already report done.
     ------------------------------------------------------- */
  function realStartPage(file) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return fetch(POST_JOBS, { method: 'POST', body: fd }).then((res) => {
      if (!res.ok) {
        return res.json().catch(() => ({})).then((body) => {
          throw new Error('POST /jobs → ' + res.status + (body && body.detail ? ': ' + body.detail : ''));
        });
      }
      return res.json(); // { job_id }
    });
  }

  function realPollPage(jobId) {
    return fetch(POLL_JOB(jobId)).then((res) => {
      if (!res.ok) throw new Error('GET /jobs/' + jobId + ' → ' + res.status);
      return res.json(); // { job_id, filename, status, result }
    });
  }

  /* Adapter: both paths resolve to { status, progress, result?, error? } */
  const STATUS_ALIASES = {
    queued: 'queued', waiting: 'queued',
    upload: 'uploading', uploading: 'uploading', in_flight: 'uploading',
    process: 'processing', processing: 'processing',
    ok: 'done', complete: 'done', completed: 'done', done: 'done',
    fail: 'error', failed: 'error', error: 'error'
  };
  function normalizePage(raw) {
    const status = STATUS_ALIASES[String(raw.status || '').toLowerCase().trim()] || 'processing';
    if (status === 'error') {
      const err = (raw.result && raw.result.error) || raw.error || 'OCR failed';
      return { status: 'error', progress: 0, error: err };
    }
    if (status !== 'done') {
      return { status: status === 'queued' ? 'queued' : (status === 'uploading' ? 'uploading' : 'processing'),
        progress: typeof raw.progress === 'number' ? raw.progress : undefined };
    }
    // result: {pages:[pageDoc…]} | pageDoc | null
    const result = raw.result;
    const pages = result && Array.isArray(result.pages) ? result.pages : null;
    const doc = pages ? pages[0] : (result && (result.lines || result.text) ? result : null);
    return { status: 'done', progress: 100, result: doc };
  }

  function startPage(file, opts) { return (MOCK ? mockStartPage(file, opts) : realStartPage(file)); }
  function pollPage(jobId) {
    return (MOCK ? mockPollPage(jobId) : realPollPage(jobId)).then(normalizePage);
  }

  /* -------------------------------------------------------
     UI
     ------------------------------------------------------- */
  const root = document.getElementById('upload-root');
  if (!root) { console.warn('[upload] mount #upload-root not found — slice 3 not rendered'); return; }
  root.classList.add('up-active'); // page 1 takes the viewport; the router slice hands over later

  const state = { pages: new Map(), running: false };
  let order = []; // display order of page keys
  let uid = 0;

  function h(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  /* --- static skeleton (layout plan zones 2 + 3) --- */
  function buildSkeleton() {
    const layout = h('div', 'up-layout');
    root.appendChild(layout);

    const card = h('section', 'card up-card');
    card.setAttribute('aria-label', 'Upload scans');
    card.appendChild(h('h2', null, 'Upload scans'));

    /* 2a dropzone */
    const dz = h('div', 'up-dz');
    dz.id = 'up-dz';
    const dzHead = h('div', 'up-dz-head');
    dzHead.appendChild(h('span', 'up-glyph', '⇪'));
    dzHead.appendChild(h('span', null, 'Drag files here'));
    dz.appendChild(dzHead);
    const browse = h('button', 'btn btn--soft', 'Browse');
    browse.type = 'button';
    browse.setAttribute('aria-label', 'Browse for scans');
    dz.appendChild(browse);
    const input = h('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = ACCEPT_ATTR;
    input.id = 'up-input';
    input.tabIndex = -1;
    input.setAttribute('aria-hidden', 'true');
    dz.appendChild(input);
    card.appendChild(dz);

    /* 2b hint */
    card.appendChild(h('p', 'up-hint', 'PDF · JPG · PNG · TIFF · multi-page OK'));

    /* rejection notices — ui.css toast, error variant */
    const notices = h('div', 'up-notices');
    notices.id = 'up-notices';
    card.appendChild(notices);

    /* 2c file list */
    const list = h('ul', 'up-list');
    list.id = 'up-list';
    list.setAttribute('aria-live', 'polite');
    list.setAttribute('aria-label', 'Scans and their status');
    card.appendChild(list);

    /* 2d primary action — the only filled button on the page */
    const start = h('button', 'btn btn--primary up-start', 'Start OCR →');
    start.type = 'button';
    start.id = 'up-start';
    start.disabled = true;
    card.appendChild(start);

    /* zone 3 help panel */
    const help = h('section', 'card up-help');
    help.setAttribute('aria-label', 'How to use');
    const ol1 = h('ol', 'up-steps');
    ['Drop your scans', 'Start OCR', 'Review flagged words'].forEach((t) => ol1.appendChild(h('li', null, t)));
    const ol2 = h('ol', 'up-steps');
    ['Fast OCR pass → page badge', 'Repair only damaged pages', 'Side-by-side review', 'Searchable Tamil PDF'].forEach((t) => ol2.appendChild(h('li', null, t)));
    help.appendChild(h('h3', null, 'How to use'));
    help.appendChild(ol1);
    help.appendChild(h('hr', 'up-rule'));
    help.appendChild(h('h3', null, 'How it works'));
    help.appendChild(ol2);
    help.appendChild(h('p', 'up-offline', '● Runs fully offline'));

    layout.appendChild(card);
    layout.appendChild(help);
    return { dz, browse, input, list, start, notices };
  }

  const el = buildSkeleton();

  /* --- status vocabulary: ui.css pill, dot-or-spinner + icon + word --- */
  const STATUS_META = {
    queued:     { word: 'Queued',     cls: 'pill pill--tinted pill--default',    dot: 'pill__dot', ico: null },
    uploading:  { word: 'Uploading',  cls: 'pill pill--tinted up-pill-live',    dot: null,        ico: null },  /* spinner */
    processing: { word: 'Processing', cls: 'pill pill--tinted pill--processing', dot: null,       ico: null },  /* spinner */
    done:       { word: 'Done',       cls: 'pill pill--tinted pill--success',   dot: 'pill__dot', ico: '✓' },
    error:      { word: 'Error',      cls: 'pill pill--tinted pill--error',     dot: 'pill__dot', ico: '✕' }
  };

  function applyPage(key, data) {
    const p = state.pages.get(key);
    if (!p) return;
    if (data.status) p.status = data.status;
    if (typeof data.progress === 'number') p.progress = data.progress;
    if (data.result) p.result = data.result;
    if (data.error) p.error = data.error;

    p.els.row.dataset.status = p.status;
    const meta = STATUS_META[p.status] || STATUS_META.queued;
    p.els.badge.className = meta.cls;
    p.els.badge.title = meta.word;
    p.els.badge.replaceChildren();
    if (meta.dot) p.els.badge.appendChild(h('span', meta.dot));
    else p.els.badge.appendChild(h('span', 'up-spin'));
    if (meta.ico) p.els.badge.appendChild(h('span', 'up-glyph', meta.ico));
    p.els.badge.appendChild(h('span', null, meta.word));

    if (p.status === 'uploading') p.els.fill.style.width = Math.max(4, p.progress || 0) + '%';
    else if (p.status === 'processing' || p.status === 'done') p.els.fill.style.width = '100%';

    p.els.why.textContent = p.status === 'error' ? (p.error || 'OCR failed') : '';
    refreshStart();
  }

  function addFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const rejected = files.filter((f) => !isAccepted(f));
    const accepted = files.filter(isAccepted);
    rejected.slice(0, 3).forEach((f) => addNotice(f.name + ': unsupported type — PDF, JPG, PNG or TIFF only'));
    if (rejected.length > 3) addNotice((rejected.length - 3) + ' more files skipped (unsupported type)');
    accepted.forEach((f) => addPage(f));
    refreshStart();
  }

  function isAccepted(f) {
    const name = (f.name || '').toLowerCase();
    return ACCEPT_MIME.includes(f.type) || ACCEPT_EXT.some((e) => name.endsWith(e));
  }

  function addPage(file) {
    const key = 'up' + (++uid);
    file.__upKey = key;

    const row = h('li', 'up-row');
    row.dataset.status = 'queued';
    row.dataset.key = key;

    const meta = h('div', 'up-meta');
    meta.appendChild(h('span', 'up-name', file.name));
    meta.appendChild(h('span', 'up-size', fmtSize(file.size)));
    row.appendChild(meta);

    const bar = h('div', 'up-bar');
    const fill = h('span', 'up-fill');
    bar.appendChild(fill);
    row.appendChild(bar);

    const why = h('span', 'up-why');
    row.appendChild(why);

    const badge = h('span', STATUS_META.queued.cls);
    badge.appendChild(h('span', 'pill__dot'));
    badge.appendChild(h('span', null, STATUS_META.queued.word));
    row.appendChild(badge);

    const del = h('button', 'btn btn--icon btn--sm btn--ghost', '×');
    del.type = 'button';
    del.setAttribute('aria-label', 'Remove ' + file.name);
    row.appendChild(del);

    el.list.appendChild(row);
    state.pages.set(key, {
      key, file, name: file.name, size: file.size,
      status: 'queued', progress: 0, jobId: null, result: null, error: null,
      els: { row, fill, why, badge }
    });
    order.push(key);
    del.addEventListener('click', () => removePage(key));
  }

  function removePage(key) {
    const p = state.pages.get(key);
    if (!p) return;
    p.els.row.remove();
    state.pages.delete(key);
    const i = order.indexOf(key);
    if (i >= 0) order.splice(i, 1);
    refreshStart();
  }

  function refreshStart() {
    const anyQueued = order.some((k) => state.pages.get(k).status === 'queued');
    el.start.disabled = state.running || !anyQueued;
    el.start.textContent = state.running ? 'OCR running…' : 'Start OCR →';
  }

  function addNotice(text) {
    const t = h('div', 'toast toast--error');
    t.setAttribute('role', 'status');
    t.appendChild(h('span', 'toast__icon', '!'));
    t.appendChild(h('span', 'toast__message', text));
    el.notices.appendChild(t);
    while (el.notices.children.length > 3) el.notices.firstElementChild.remove();
  }

  /* --- Start OCR: POST /jobs per file, then poll GET /jobs/{id} --- */
  function startBatch() {
    const keys = order.filter((k) => state.pages.get(k).status === 'queued');
    if (!keys.length || state.running) return;
    state.running = true;
    refreshStart();

    let pending = keys.length;
    keys.forEach((key, i) => {
      const p = state.pages.get(key);
      startPage(p.file, { fail: keys.length > 1 && i === keys.length - 1, delay: 250 + i * 350 })
        .then(({ job_id }) => { p.jobId = job_id; })
        .catch((err) => applyPage(key, { status: 'error', error: err.message }))
        .finally(() => { if (--pending === 0) pollLoop(keys, 0); });
    });
  }

  function pollLoop(keys, depth) {
    const active = keys.filter((k) => {
      const p = state.pages.get(k);
      return p && p.jobId && p.status !== 'done' && p.status !== 'error';
    });
    if (!active.length || depth >= POLL_MAX) { state.running = false; refreshStart(); return; }
    let left = active.length;
    active.forEach((key) => {
      pollPage(state.pages.get(key).jobId)
        .then((pg) => applyPage(key, pg))
        .catch((err) => applyPage(key, { status: 'error', error: err.message }))
        .finally(() => { if (--left === 0) setTimeout(() => pollLoop(keys, depth + 1), POLL_MS); });
    });
  }

  /* --- dropzone wiring --- */
  let dragDepth = 0;
  el.dz.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth++; el.dz.classList.add('is-drag'); });
  el.dz.addEventListener('dragover', (e) => { e.preventDefault(); });
  el.dz.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; el.dz.classList.remove('is-drag'); } });
  el.dz.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    el.dz.classList.remove('is-drag');
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  el.dz.addEventListener('click', (e) => {
    if (e.target.closest('button, input, label')) return; // Browse handles itself
    el.input.click();
  });
  el.browse.addEventListener('click', (e) => { e.stopPropagation(); el.input.click(); });
  el.input.addEventListener('change', () => { addFiles(el.input.files); el.input.value = ''; });
  el.start.addEventListener('click', startBatch);

  refreshStart();
})();
