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
     Contract: schema/endpoints.md (fastapi/sqlite, app 0.2.0).
       GET  /health          → {ok, ocr_engine: paddle|stub|not_initialized, ocr_error?}
       POST /jobs  (form field "file", ONE file) → {job_id} | 400/422 {detail}
       GET  /jobs/{job_id}   → {status: pending|done|error,
                                result: {pages:[…]} | {error} | null} | 404
     POST is synchronous today (job is already done|error when it
     returns); we still poll while status is "pending".

     LIVE is the default. The mock stays for offline demos:
       index.html?mock=1        → mock engine (schema/doc_demo.json)
       MOCK_DEFAULT = true      → mock without the query param
     API origin: same origin as the page by default. For a backend on
     another origin (needs CORS on the backend) use ?api=http://127.0.0.1:8000
     ======================================================= */
  const MOCK_DEFAULT = false;
  const QS = new URLSearchParams(location.search);
  const MOCK = QS.has('mock') ? QS.get('mock') !== '0' : MOCK_DEFAULT;
  const API_BASE = (QS.get('api') || '').replace(/\/+$/, '');

  const HEALTH = API_BASE + '/health';
  const POST_JOBS = API_BASE + '/jobs';
  const POLL_JOB = (id) => `${API_BASE}/jobs/${encodeURIComponent(id)}`;
  const POLL_MS = 400;
  const POLL_MAX = 600; // depth cap ≈ 4 min per job
  const LIVE_PARALLEL = 2; // OCR runs inside POST: keep the server load small

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
     synchronously, so the first poll normally reports done.
     ------------------------------------------------------- */
  class ApiError extends Error {
    constructor(message, kind) { super(message); this.kind = kind; } // kind: 'down' | 'http'
  }
  const MIME_BY_EXT = { '.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.tif': 'image/tiff', '.tiff': 'image/tiff' };
  function detailText(body) {
    if (!body || body.detail == null) return '';
    if (typeof body.detail === 'string') return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d) => (d && d.msg) || '').filter(Boolean).join('; ');
    return String(body.detail);
  }

  /* The backend checks content type AND extension. Browsers send an
     empty type for some TIFFs; send the type the extension implies. */
  function withMime(file) {
    if (ACCEPT_MIME.includes(file.type)) return file;
    const name = (file.name || '').toLowerCase();
    const ext = ACCEPT_EXT.find((e) => name.endsWith(e));
    return ext ? new File([file], file.name, { type: MIME_BY_EXT[ext] }) : file;
  }

  /* XHR (not fetch) so the row shows real upload progress. When the
     bytes are sent the server starts OCR: that is "processing". */
  function realStartPage(file, opts) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append('file', withMime(file), file.name);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', POST_JOBS);
      xhr.responseType = 'text';
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && opts.onProgress) opts.onProgress('uploading', Math.round((e.loaded / e.total) * 100));
      };
      xhr.upload.onload = () => { if (opts.onProgress) opts.onProgress('processing', 100); };
      xhr.onerror = () => reject(new ApiError('Backend unreachable (POST /jobs)', 'down'));
      xhr.ontimeout = () => reject(new ApiError('Backend timed out (POST /jobs)', 'down'));
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && body && body.job_id) { resolve({ job_id: body.job_id }); return; }
        const d = detailText(body);
        if (xhr.status === 0) { reject(new ApiError('Backend unreachable (POST /jobs)', 'down')); return; }
        reject(new ApiError((d ? d.charAt(0).toUpperCase() + d.slice(1) : 'Upload failed') + ' (' + xhr.status + ')', 'http'));
      };
      if (opts.onProgress) opts.onProgress('uploading', 4);
      xhr.send(fd);
    });
  }

  function realPollPage(jobId) {
    return fetch(POLL_JOB(jobId), { cache: 'no-store' })
      .catch(() => { throw new ApiError('Backend unreachable (GET /jobs)', 'down'); })
      .then((res) => {
        if (res.status === 404) throw new ApiError('Job not found on the server', 'http');
        if (!res.ok) throw new ApiError('GET /jobs → ' + res.status, 'http');
        return res.json(); // { job_id, filename, sha256, status, created_at, result }
      });
  }

  /* /health → 'paddle' | 'stub' | 'not_initialized' | 'down' */
  function realHealth() {
    return fetch(HEALTH, { cache: 'no-store' })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
      .then((b) => ({ state: b && b.ok ? String(b.ocr_engine || 'unknown') : 'down', error: b && b.ocr_error }))
      .catch(() => ({ state: 'down' }));
  }

  /* Adapter: both paths resolve to { status, progress, result?, error? } */
  const STATUS_ALIASES = {
    queued: 'queued', waiting: 'queued',
    upload: 'uploading', uploading: 'uploading', in_flight: 'uploading',
    process: 'processing', processing: 'processing',
    pending: 'processing', running: 'processing',
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
    return { status: 'done', progress: 100, result: doc, pages: pages || (doc ? [doc] : []) };
  }

  function startPage(file, opts) { return (MOCK ? mockStartPage(file, opts) : realStartPage(file, opts)); }
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
    const offline = h('p', 'up-offline', '● Runs fully offline');
    help.appendChild(offline);

    layout.appendChild(card);
    layout.appendChild(help);
    return { dz, browse, input, list, start, notices, offline };
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
    if (data.pages) p.pages = data.pages;
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
    p.els.size.textContent = fmtSize(p.size) + (p.status === 'done' ? pageSummary(p.pages) : '');
    refreshStart();
  }

  /* "· 3 pages · 1 to review" from result.pages[] */
  function pageSummary(pages) {
    if (!pages || !pages.length) return '';
    const review = pages.filter((pg) => pg && pg.needs_review).length;
    return ' · ' + pages.length + (pages.length === 1 ? ' page' : ' pages') + (review ? ' · ' + review + ' to review' : '');
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
    const size = h('span', 'up-size', fmtSize(file.size));
    meta.appendChild(size);
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
      pages: null,
      els: { row, fill, why, badge, size }
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
    if (MOCK) { startMockBatch(keys); return; }
    probeHealth().then((h) => {
      if (h.state === 'down') {
        keys.forEach((key) => applyPage(key, { status: 'error', error: 'Backend unreachable: nothing was uploaded' }));
        addNotice('Backend unreachable at ' + (API_BASE || location.origin) + ' - start the API, or open ?mock=1 for the demo');
        state.running = false; refreshStart();
        return;
      }
      runLive(keys);
    });
  }

  /* Mock: unchanged demo timing (all files start, then poll). */
  function startMockBatch(keys) {
    let pending = keys.length;
    keys.forEach((key, i) => {
      const p = state.pages.get(key);
      startPage(p.file, { fail: keys.length > 1 && i === keys.length - 1, delay: 250 + i * 350 })
        .then(({ job_id }) => { p.jobId = job_id; })
        .catch((err) => applyPage(key, { status: 'error', error: err.message }))
        .finally(() => { if (--pending === 0) pollLoop(keys, 0); });
    });
  }

  /* Live: at most LIVE_PARALLEL uploads at once; each row goes
     uploading (real bytes) -> processing (server OCR) -> done|error. */
  function runLive(keys) {
    const queue = keys.slice();
    let inFlight = 0;
    let wentDown = false;
    const next = () => {
      if (!queue.length) { if (inFlight === 0) pollLoop(keys, 0); return; }
      const key = queue.shift();
      const p = state.pages.get(key);
      if (!p) { next(); return; } // removed meanwhile
      inFlight++;
      startPage(p.file, { onProgress: (status, progress) => applyPage(key, { status, progress }) })
        .then(({ job_id }) => { p.jobId = job_id; applyPage(key, { status: 'processing', progress: 100 }); return pollPage(job_id); })
        .then((pg) => { if (pg) applyPage(key, pg); })
        .catch((err) => {
          applyPage(key, { status: 'error', error: err.message });
          if (err.kind === 'down' && !wentDown) { wentDown = true; setHealth({ state: 'down' }); addNotice(p.name + ': ' + err.message); }
          else if (err.kind !== 'down') addNotice(p.name + ': ' + err.message);
        })
        .finally(() => { inFlight--; next(); });
    };
    for (let i = 0; i < LIVE_PARALLEL; i++) next();
  }

  function pollLoop(keys, depth) {
    const active = keys.filter((k) => {
      const p = state.pages.get(k);
      return p && p.jobId && p.status !== 'done' && p.status !== 'error';
    });
    if (!active.length || depth >= POLL_MAX) {
      // Never leave a spinner behind: anything still open has timed out.
      active.forEach((k) => applyPage(k, { status: 'error', error: 'Timed out waiting for OCR' }));
      state.running = false; refreshStart(); return;
    }
    let left = active.length;
    active.forEach((key) => {
      pollPage(state.pages.get(key).jobId)
        .then((pg) => applyPage(key, pg))
        .catch((err) => applyPage(key, { status: 'error', error: err.message }))
        .finally(() => { if (--left === 0) setTimeout(() => pollLoop(keys, depth + 1), POLL_MS); });
    });
  }

  /* --- connection state: the help panel's "offline" line tells the truth --- */
  const HEALTH_TEXT = {
    mock: '● Demo mode: mock data, runs fully offline',
    paddle: '● Connected: OCR engine ready',
    stub: '● Connected: stub OCR, text is placeholder',
    not_initialized: '● Connected: OCR engine starting',
    down: '● Backend unreachable',
    unknown: '● Connected',
    checking: '● Checking backend…'
  };
  function setHealth(h) {
    const line = el.offline;
    if (!line) return;
    const state_ = HEALTH_TEXT[h.state] ? h.state : 'unknown';
    line.dataset.state = state_;
    line.textContent = HEALTH_TEXT[state_];
    line.title = h.state === 'down'
      ? 'No response from ' + (API_BASE || location.origin) + '/health. Start the API or open with ?mock=1.'
      : (h.error ? 'OCR engine: ' + h.error : '');
  }
  function probeHealth() {
    setHealth({ state: 'checking' });
    return realHealth().then((h) => { setHealth(h); return h; });
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
  if (MOCK) setHealth({ state: 'mock' }); else probeHealth();
})();
