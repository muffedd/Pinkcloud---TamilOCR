/* =========================================================
   Pink Cloud — slice 3: Upload screen (page 1, layout plan zone 2)
   Mount: <div id="upload-root"> in index.html.
   Vanilla JS. No packages, no network beyond the API seam.

   Status flow per page: queued -> uploading -> processing -> done|error
   Design: v2 (stepper, gradient drop zone, file rows, Prev/Next), brand orange.
   Files upload as soon as they are added. Done rows offer: open in editor,
   Download result PDF (GET /jobs/{id}/export.pdf), remove.
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
  // Scripted drag-over -> drop demo: only with ?mock=1&demo, never on the live path.
  const DEMO = MOCK && QS.has('demo');
  const API_BASE = (QS.get('api') || '').replace(/\/+$/, '');

  const HEALTH = API_BASE + '/health';
  const POST_JOBS = API_BASE + '/jobs';
  const POLL_JOB = (id) => `${API_BASE}/jobs/${encodeURIComponent(id)}`;
  /* Result PDF, page-for-page (route from the backend side; lands on main). */
  const EXPORT_PDF = (id) => `${API_BASE}/jobs/${encodeURIComponent(id)}/export.pdf`;
  /* Upload -> editor handoff. The editor loads GET /jobs/{job_id}
     (result.pages) itself; we only pass the ids. */
  /* "Try a sample page": drop a real scan at this path and the link
     appears on the live page (it stays hidden until the file exists).
     In mock mode the link always shows and runs the mock engine. */
  const SAMPLE_URL = './samples/sample-page.png';
  const SAMPLE_NAME = 'sample-kural-page.png';
  function EDITOR_URL(jobIds) {
    const q = new URLSearchParams();
    if (MOCK) q.set('mock', '1');
    if (jobIds.length) q.set('job', jobIds[0]);
    if (jobIds.length > 1) q.set('jobs', jobIds.join(','));
    if (API_BASE) q.set('api', API_BASE);
    const s = q.toString();
    return './editor.html' + (s ? '?' + s : '');
  }
  const POLL_MS = 400;
  const POLL_MAX = 600; // depth cap ≈ 4 min per job
  const LIVE_PARALLEL = 2; // OCR runs inside POST: keep the server load small

  const ACCEPT_EXT = ['.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.zip'];
  const ACCEPT_MIME = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff',
    'application/zip', 'application/x-zip-compressed'];
  const ACCEPT_ATTR = '.pdf,.jpg,.jpeg,.png,.tif,.tiff,.zip';
  /* ZIP of page images -> ONE job, one page per image (POST /jobs with a
     repeated `files` field). Same image types + limit as the backend's
     multi-image path (main.py _read_multi / MAX_FILES_PER_JOB). */
  const ZIP_IMG_EXT = ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'];
  const ZIP_MAX_IMAGES = 100;

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
    text: 'அகர முதல எழுத்தெல்லாம் ஆதி\nபகவன் முதற்றே உலகு\nகற்றதனால் ஆய பயனென்கொல் வாழறிவன்',
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
    '.png': 'image/png', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.webp': 'image/webp' };

  function isZip(file) {
    return /\.zip$/i.test(file.name || '') || file.type === 'application/zip' || file.type === 'application/x-zip-compressed';
  }

  /* Minimal ZIP reader, no library: central directory -> entries; stored
     (method 0) or deflate (method 8, via the browser's DecompressionStream).
     No ZIP64, no encryption: those entries are reported, never guessed. */
  async function unzipImages(file) {
    const buf = new Uint8Array(await file.arrayBuffer());
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    let eocd = -1;
    for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 65535); i--) {
      if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new ApiError('Not a readable ZIP file', 'http');
    const count = dv.getUint16(eocd + 10, true);
    let off = dv.getUint32(eocd + 16, true);
    if (off === 0xffffffff) throw new ApiError('ZIP64 archives are not supported: re-zip without ZIP64', 'http');
    const utf8 = new TextDecoder('utf-8');
    const cp437 = new TextDecoder('latin1');
    const images = [];
    let skipped = 0, locked = 0;
    for (let n = 0; n < count; n++) {
      if (off + 46 > buf.length || dv.getUint32(off, true) !== 0x02014b50) throw new ApiError('ZIP directory is damaged', 'http');
      const flags = dv.getUint16(off + 8, true);
      const method = dv.getUint16(off + 10, true);
      const csize = dv.getUint32(off + 20, true);
      const nlen = dv.getUint16(off + 28, true), xlen = dv.getUint16(off + 30, true), clen = dv.getUint16(off + 32, true);
      const lho = dv.getUint32(off + 42, true);
      const raw = buf.subarray(off + 46, off + 46 + nlen);
      const path = (flags & 0x800 ? utf8 : cp437).decode(raw);
      off += 46 + nlen + xlen + clen;
      const base = path.split('/').pop();
      if (!base || path.endsWith('/') || path.startsWith('__MACOSX/') || base.startsWith('.')) continue; // folders, macOS junk
      const ext = (base.match(/\.[^.]+$/) || [''])[0].toLowerCase();
      if (!ZIP_IMG_EXT.includes(ext)) { skipped++; continue; }
      if (flags & 1) { locked++; continue; }
      if (csize === 0xffffffff) throw new ApiError('ZIP64 archives are not supported: re-zip without ZIP64', 'http');
      const dataAt = lho + 30 + dv.getUint16(lho + 26, true) + dv.getUint16(lho + 28, true);
      const comp = buf.subarray(dataAt, dataAt + csize);
      let bytes;
      if (method === 0) bytes = comp;
      else if (method === 8 && typeof DecompressionStream === 'function') {
        const stream = new Blob([comp]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
        bytes = new Uint8Array(await new Response(stream).arrayBuffer());
      } else throw new ApiError(base + ': unsupported ZIP compression (use a standard ZIP)', 'http');
      images.push({ path, file: new File([bytes], base, { type: MIME_BY_EXT[ext] }) });
    }
    /* Page order = file name order, numbers compared as numbers (p2 < p10). */
    images.sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true, sensitivity: 'base' }));
    if (!images.length) {
      throw new ApiError('No page images in this ZIP' + (locked ? ' (password-protected entries skipped)' : '') +
        '. Use JPG, PNG, TIFF or WEBP; put PDFs in on their own', 'http');
    }
    if (images.length > ZIP_MAX_IMAGES) throw new ApiError('Too many images in this ZIP: at most ' + ZIP_MAX_IMAGES + ' per job', 'http');
    return { files: images.map((x) => x.file), skipped, locked };
  }
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
      if (opts.files) opts.files.forEach((f) => fd.append('files', f, f.name)); // ZIP: one job, page per image
      else fd.append('file', withMime(file), file.name);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', POST_JOBS);
      xhr.responseType = 'text';
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && opts.onProgress) opts.onProgress('uploading', Math.round((e.loaded / e.total) * 100));
      };
      xhr.upload.onload = () => { if (opts.onProgress) opts.onProgress('processing', 100); };
      xhr.onerror = () => reject(new ApiError('Backend unreachable (POST /jobs)', 'down'));
      xhr.onabort = () => reject(new ApiError('Upload cancelled', 'cancel'));
      if (opts.onXhr) opts.onXhr(xhr);
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
     UI — v2 design (stepper, gradient drop zone, file rows,
     Remove all, Previous/Next). Files upload as soon as they
     are added; Next step opens the editor with the job ids.
     ------------------------------------------------------- */
  const root = document.getElementById('upload-root');
  if (!root) { console.warn('[upload] mount #upload-root not found — upload screen not rendered'); return; }
  root.classList.add('up-active');

  const state = { pages: new Map(), inFlight: 0, queue: [] };
  let order = []; // display order of page keys
  let uid = 0;

  function h(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function svgEl(markup) {
    const t = document.createElement('template');
    t.innerHTML = markup.trim();
    return t.content.firstChild;
  }
  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  /* --- icons (static markup, brand orange) --- */
  const CHECK = (c) => `<svg width="16" height="16" viewBox="0 0 16 16"><path d="M2.6 8.4l3.4 3.3 7.3-7.3" fill="none" stroke="${c}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const EYE = '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M1.2 9s2.9-5.2 7.8-5.2S16.8 9 16.8 9s-2.9 5.2-7.8 5.2S1.2 9 1.2 9z" stroke-linejoin="round"/><circle cx="9" cy="9" r="2.4"/></svg>';
  const BIN = '<svg width="16" height="17" viewBox="0 0 16 17" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 3.5h13M5.5 3.5V2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M3 3.5l.8 11a1.5 1.5 0 0 0 1.5 1.4h5.4a1.5 1.5 0 0 0 1.5-1.4l.8-11"/></svg>';
  const XC = '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="9" cy="9" r="7.6"/><path d="M6.3 6.3l5.4 5.4M11.7 6.3l-5.4 5.4"/></svg>';
  const DL = '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2.2v9.2M5.2 7.8L9 11.6l3.8-3.8M2.5 12.6v1.6a1.6 1.6 0 0 0 1.6 1.6h9.8a1.6 1.6 0 0 0 1.6-1.6v-1.6"/></svg>';
  const BDG_OK = '<span class="up-bdg"><svg viewBox="0 0 10 10" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1.8 5.3l2.1 2.1 4.3-4.6"/></svg></span>';
  let icoSeq = 0;
  function fileIcon(name) {
    const id = 'upi' + (++icoSeq);
    if (/\.(png|jpe?g|tiff?)$/i.test(name)) {
      return `<svg width="36" height="30" viewBox="0 0 36 30" aria-hidden="true"><defs><linearGradient id="g${id}" x1="0" y1="0" x2="0.6" y2="1"><stop offset="0" stop-color="#FB8D69"/><stop offset="1" stop-color="#F94612"/></linearGradient><clipPath id="c${id}"><rect width="36" height="30" rx="4"/></clipPath></defs><g clip-path="url(#c${id})"><rect width="36" height="30" fill="url(#g${id})"/><circle cx="10" cy="9" r="4.2" fill="#FFC9B3"/><path d="M-2 30 L12 17 L24 30z" fill="#FFF4EF"/><path d="M8 30 L24 13 L40 30z" fill="#D93A0B"/></g></svg>`;
    }
    return `<svg width="34" height="40" viewBox="0 0 34 40" aria-hidden="true"><defs><linearGradient id="g${id}" x1="0" y1="0" x2="0.35" y2="1"><stop offset="0" stop-color="#FB8D69"/><stop offset="1" stop-color="#F94612"/></linearGradient></defs><path d="M5 0h15l14 14v21a5 5 0 0 1-5 5H5a5 5 0 0 1-5-5V5a5 5 0 0 1 5-5z" fill="url(#g${id})"/><path d="M20 0l14 14h-10a4 4 0 0 1-4-4z" fill="#D93A0B"/></svg>`;
  }

  /* --- static skeleton --- */
  function buildSkeleton() {
    const page = h('div', 'up-page');
    root.appendChild(page);

    /* stepper: the web flow as the user sees it; this screen is step 1 */
    const stepper = h('ol', 'up-stepper');
    stepper.setAttribute('aria-label', 'Progress');
    stepper.appendChild(h('li', 'up-seg c'));
    [
      { lbl: 'Upload scans', x: 64, cls: 'is-current', n: 1 },
      { lbl: 'Tamil OCR', x: 494, cls: '', n: 2 },
      { lbl: 'Review & export', x: 924, cls: '', n: 3 }
    ].forEach((s) => {
      const li = h('li', 'up-step ' + s.cls);
      li.style.left = s.x + 'px';
      if (s.c) li.style.setProperty('--c', s.c);
      if (s.cls === 'is-current') li.setAttribute('aria-current', 'step');
      const dot = h('div', 'up-dot', s.n ? String(s.n) : undefined);
      if (!s.n) dot.appendChild(svgEl(CHECK(s.c)));
      li.appendChild(dot);
      li.appendChild(h('div', 'up-lbl', s.lbl));
      stepper.appendChild(li);
    });
    page.appendChild(stepper);

    const card = h('section', 'up-card');
    card.setAttribute('aria-label', 'Upload scans');

    /* drop zone */
    const dz = h('div', 'up-dz');
    dz.id = 'up-dz';
    dz.appendChild(svgEl('<svg class="up-dz-border" preserveAspectRatio="none" aria-hidden="true"><rect x="0.7" y="0.7" rx="8.5" ry="8.5" width="calc(100% - 1.4px)" height="calc(100% - 1.4px)"/></svg>'));
    /* palm-leaf (olai) strips on both flanks: decoration only */
    const leaves = h('div', 'up-leaves');
    leaves.setAttribute('aria-hidden', 'true');
    const KURAL = [
      ['அகர முதல எழுத்தெல்லாம் ஆதி', 'பகவன் முதற்றே உலகு'],
      ['கற்றதனால் ஆய பயனென்கொல்', 'வாலறிவன் நற்றாள் தொழாஅர்'],
      ['மலர்மிசை ஏகினான் மாணடி', 'சேர்ந்தார் நிலமிசை நீடுவாழ்வார்']
    ];
    // [side, top px, rotate deg]
    [['l', 70, -6], ['l', 142, -3], ['l', 214, -7], ['r', 58, 5], ['r', 132, 2], ['r', 206, 6]].forEach((pos, k) => {
      const leaf = h('div', 'up-leaf up-leaf--' + pos[0] + (k % 3 === 1 ? ' is-near' : ''));
      leaf.style.top = pos[1] + 'px';
      leaf.style.setProperty('--r', pos[2] + 'deg');
      leaf.appendChild(h('span', 'up-leaf-t', KURAL[k % 3][0]));
      leaf.appendChild(h('span', 'up-leaf-b', KURAL[k % 3][1]));
      leaf.appendChild(h('i', 'up-leaf-hole'));
      leaves.appendChild(leaf);
    });
    dz.appendChild(leaves);

    const idle = h('div', 'up-idle');
    const tile = h('div', 'up-tile');
    tile.appendChild(svgEl('<svg width="32" height="37" viewBox="0 0 32 37" aria-hidden="true"><defs><linearGradient id="up-tg" x1="0" y1="0" x2="0.4" y2="1"><stop offset="0" stop-color="#FB8D69"/><stop offset="1" stop-color="#F94612"/></linearGradient></defs><path d="M5 0h14l13 13v19a5 5 0 0 1-5 5H5a5 5 0 0 1-5-5V5a5 5 0 0 1 5-5z" fill="url(#up-tg)"/><path d="M19 0l13 13H23a4 4 0 0 1-4-4z" fill="#D93A0B"/></svg>'));
    idle.appendChild(tile);
    idle.appendChild(h('h3', null, 'Drag & drop your scans here'));
    idle.appendChild(h('div', 'up-or', 'or'));
    const browse = h('button', 'up-browse', 'Browse files');
    browse.type = 'button';
    browse.setAttribute('aria-label', 'Browse for scans');
    idle.appendChild(browse);
    const sample = h('button', 'up-sample', 'Try a sample page');
    sample.type = 'button';
    sample.id = 'up-sample';
    sample.hidden = !MOCK; // live: shown once SAMPLE_URL is found
    idle.appendChild(sample);
    const note = h('div', 'up-note');
    const pasteKey = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? '⌘V' : 'Ctrl+V';
    note.appendChild(document.createTextNode('Supported files: PDF (multi-page), ZIP of page images, JPG, PNG and TIFF · or paste an image (' + pasteKey + ')'));
    note.appendChild(h('br'));
    note.appendChild(h('span', 'ta', 'தமிழ் ஆவணங்களுக்கான OCR'));
    idle.appendChild(note);
    dz.appendChild(idle);

    const over = h('div', 'up-over');
    over.setAttribute('aria-hidden', 'true');
    over.appendChild(svgEl('<svg class="up-ob" preserveAspectRatio="none"><rect x="0.8" y="0.8" rx="8.5" ry="8.5" width="calc(100% - 1.6px)" height="calc(100% - 1.6px)"/></svg>'));
    const fx = h('div', 'up-fx');
    fx.appendChild(svgEl('<svg viewBox="0 0 140 74" width="140" height="74"><path d="M64 30h8l6 6v14a2.5 2.5 0 0 1-2.5 2.5h-11.5a2.5 2.5 0 0 1-2.5-2.5V32.5A2.5 2.5 0 0 1 64 30z" fill="none" stroke="#fff" stroke-width="2.2" stroke-linejoin="round"/><path d="M72 30v4.5a1.5 1.5 0 0 0 1.5 1.5H78" fill="none" stroke="#fff" stroke-width="2.2" stroke-linejoin="round"/><path class="up-arc" pathLength="1" d="M40 8 C 30 16, 29 32, 38 40 C 42 44, 46 46, 51 47"/><path class="up-head" d="M45.5 41.5 L51 47 L44 50"/><path class="up-arc" pathLength="1" d="M100 8 C 110 16, 111 32, 102 40 C 98 44, 94 46, 89 47"/><path class="up-head" d="M94.5 41.5 L89 47 L96 50"/></svg>'));
    over.appendChild(fx);
    over.appendChild(h('h4', null, 'Drop your scans here'));
    dz.appendChild(over);

    const input = h('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = ACCEPT_ATTR;
    input.id = 'up-input';
    input.hidden = true;
    input.tabIndex = -1;
    input.setAttribute('aria-hidden', 'true');
    dz.appendChild(input);
    card.appendChild(dz);

    /* rejection notices */
    const notices = h('div', 'up-notices');
    notices.id = 'up-notices';
    notices.setAttribute('role', 'status');
    card.appendChild(notices);

    /* files */
    const fhead = h('div', 'up-fhead');
    const title = h('h5', null, 'Files');
    const count = h('span', null, '');
    title.appendChild(count);
    fhead.appendChild(title);
    const rmall = h('button', 'up-rmall');
    rmall.type = 'button';
    rmall.id = 'up-rmall';
    rmall.appendChild(document.createTextNode('Remove all '));
    rmall.appendChild(svgEl(BIN));
    fhead.appendChild(rmall);
    card.appendChild(fhead);

    const list = h('ul', 'up-list');
    list.id = 'up-list';
    list.setAttribute('aria-live', 'polite');
    list.setAttribute('aria-label', 'Scans and their status');
    card.appendChild(list);
    const empty = h('div', 'up-empty');
    empty.appendChild(h('p', null, 'No scans yet. Each file uploads and runs OCR as soon as you add it.'));
    const demo = h('button', 'up-demo');
    demo.type = 'button';
    demo.id = 'up-demo';
    demo.setAttribute('aria-haspopup', 'dialog');
    demo.appendChild(svgEl('<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="6.2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5.7 4.7v4.6L9.9 7z" fill="currentColor"/></svg>'));
    demo.appendChild(h('span', null, 'Watch demo'));
    empty.appendChild(demo);
    card.appendChild(empty);

    /* footer */
    const foot = h('div', 'up-foot');
    const prev = h('button', 'up-prev', 'Previous step');
    prev.type = 'button';
    prev.id = 'up-prev';
    const canGoBack = document.referrer && (() => { try { return new URL(document.referrer).origin === location.origin; } catch (e) { return false; } })();
    prev.disabled = !canGoBack;
    prev.title = canGoBack ? 'Back' : 'Upload is the first screen';
    foot.appendChild(prev);
    const offline = h('p', 'up-offline', '● Checking backend…');
    foot.appendChild(offline);
    const next = h('button', 'up-next', 'Next step');
    next.type = 'button';
    next.id = 'up-next';
    next.disabled = true;
    foot.appendChild(next);
    card.appendChild(foot);

    page.appendChild(card);
    return { dz, browse, sample, input, list, empty, notices, offline, rmall, count, prev, next, demo };
  }

  const el = buildSkeleton();

  /* --- row rendering --- */
  const PCT_TEXT = { queued: 'Waiting', processing: 'Reading…' };

  function applyPage(key, data) {
    const p = state.pages.get(key);
    if (!p) return;
    const was = p.status;
    if (data.status) p.status = data.status;
    if (typeof data.progress === 'number') p.progress = data.progress;
    if (data.result) p.result = data.result;
    if (data.pages) p.pages = data.pages;
    if (data.error) p.error = data.error;

    const { row, fill, pct, why, size, ic, acts } = p.els;
    row.dataset.status = p.status;

    if (p.status === 'uploading') {
      const v = Math.max(4, Math.min(100, p.progress || 0));
      fill.style.width = v + '%';
      pct.textContent = Math.round(v) + '%';
    } else if (p.status === 'processing') {
      fill.style.width = '100%';
      pct.textContent = PCT_TEXT.processing;
    } else if (p.status === 'queued') {
      fill.style.width = '0%';
      pct.textContent = PCT_TEXT.queued;
    }
    why.textContent = p.status === 'error' ? (p.error || 'OCR failed') : '';
    size.textContent = fmtSize(p.size) + (p.status === 'done' ? pageSummary(p.pages) : '');

    if (p.status !== was && (p.status === 'done' || p.status === 'error')) {
      ic.querySelector('.up-bdg')?.remove();
      if (p.status === 'done') {
        ic.insertAdjacentHTML('beforeend', BDG_OK);
        row.classList.remove('is-flash'); void row.offsetWidth; row.classList.add('is-flash');
      } else {
        const b = h('span', 'up-bdg is-err', '!');
        ic.appendChild(b);
      }
      renderActs(p);
    }
    refreshFooter();
  }

  function iconButton(a, label, svg) {
    const b = h('button', 'up-ib');
    b.type = 'button';
    b.dataset.a = a;
    b.title = label;
    b.setAttribute('aria-label', label);
    b.appendChild(svgEl(svg));
    return b;
  }
  function renderActs(p) {
    const acts = p.els.acts;
    acts.replaceChildren();
    if (p.status === 'done') {
      acts.appendChild(iconButton('view', 'Open ' + p.name + ' in the editor', EYE));
      if (!MOCK && p.jobId) {
        const a = h('a', 'up-ib');
        a.href = EXPORT_PDF(p.jobId);
        a.setAttribute('download', p.name.replace(/\.[^.]+$/, '') + '-tamil-ocr.pdf');
        a.title = 'Download result PDF';
        a.setAttribute('aria-label', 'Download result PDF for ' + p.name);
        a.dataset.a = 'pdf';
        a.appendChild(svgEl(DL));
        acts.appendChild(a);
      }
      acts.appendChild(iconButton('del', 'Remove ' + p.name, BIN));
    } else if (p.status === 'error') {
      acts.appendChild(iconButton('del', 'Remove ' + p.name, BIN));
    } else {
      acts.appendChild(iconButton('cancel', 'Cancel ' + p.name, XC));
    }
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
    rejected.slice(0, 3).forEach((f) => addNotice(f.name + ': unsupported type. Use PDF, ZIP, JPG, PNG or TIFF'));
    if (rejected.length > 3) addNotice((rejected.length - 3) + ' more files skipped (unsupported type)');
    const keys = accepted.map((f) => addPage(f));
    // Mock only: the last file of a multi-file drop comes back damaged (error-state demo).
    if (MOCK && keys.length > 1) state.pages.get(keys[keys.length - 1]).mockFail = true;
    enqueue(keys);
  }

  function isAccepted(f) {
    const name = (f.name || '').toLowerCase();
    return ACCEPT_MIME.includes(f.type) || ACCEPT_EXT.some((e) => name.endsWith(e));
  }

  function addPage(file) {
    const key = 'up' + (++uid);
    const row = h('li', 'up-row is-enter');
    row.dataset.status = 'queued';
    row.dataset.key = key;

    const ic = h('div', 'up-ic');
    ic.insertAdjacentHTML('beforeend', fileIcon(file.name));
    row.appendChild(ic);

    const body = h('div', 'up-body');
    const top = h('div', 'up-top');
    top.appendChild(h('span', 'up-name', file.name));
    const size = h('span', 'up-size', fmtSize(file.size));
    top.appendChild(size);
    body.appendChild(top);
    const pct = h('span', 'up-pct', PCT_TEXT.queued);
    body.appendChild(pct);
    const bar = h('div', 'up-bar');
    const fill = h('i', 'up-fill');
    bar.appendChild(fill);
    body.appendChild(bar);
    const why = h('span', 'up-why');
    body.appendChild(why);
    row.appendChild(body);

    const acts = h('div', 'up-acts');
    row.appendChild(acts);

    el.list.appendChild(row);
    requestAnimationFrame(() => requestAnimationFrame(() => row.classList.remove('is-enter')));
    const p = {
      key, file, name: file.name, size: file.size,
      status: 'queued', progress: 0, jobId: null, result: null, error: null, pages: null, xhr: null,
      els: { row, fill, pct, why, size, ic, acts }
    };
    state.pages.set(key, p);
    order.push(key);
    renderActs(p);
    refreshFooter();
    return key;
  }

  function removePage(key) {
    const p = state.pages.get(key);
    if (!p) return;
    if (p.xhr && p.status === 'uploading') { try { p.xhr.abort(); } catch (e) { /* already done */ } }
    state.pages.delete(key);
    state.queue = state.queue.filter((k) => k !== key);
    const i = order.indexOf(key);
    if (i >= 0) order.splice(i, 1);
    const row = p.els.row;
    row.style.height = row.getBoundingClientRect().height + 'px';
    requestAnimationFrame(() => row.classList.add('is-leaving'));
    setTimeout(() => row.remove(), 240);
    refreshFooter();
  }

  el.list.addEventListener('click', (e) => {
    const b = e.target.closest('.up-ib');
    if (!b || b.tagName === 'A') return;
    const row = b.closest('.up-row');
    const key = row && row.dataset.key;
    if (!key) return;
    if (b.dataset.a === 'del' || b.dataset.a === 'cancel') removePage(key);
    else if (b.dataset.a === 'view') {
      const p = state.pages.get(key);
      if (p && p.jobId) location.href = EDITOR_URL([p.jobId]);
    }
  });
  el.rmall.addEventListener('click', () => {
    order.slice().forEach((key, i) => setTimeout(() => removePage(key), i * 40));
  });

  function doneJobIds() {
    return order.map((k) => state.pages.get(k)).filter((p) => p && p.status === 'done' && p.jobId).map((p) => p.jobId);
  }
  function refreshFooter() {
    const n = order.length;
    const busy = order.some((k) => { const s = state.pages.get(k).status; return s === 'queued' || s === 'uploading' || s === 'processing'; });
    const done = doneJobIds().length;
    el.next.disabled = busy || !done;
    el.next.title = busy ? 'Waiting for OCR to finish' : (done ? 'Open the editor' : 'Add a scan first');
    el.rmall.disabled = !n;
    el.count.textContent = n ? String(n) : '';
    el.empty.hidden = n > 0;
  }
  el.next.addEventListener('click', () => {
    const ids = doneJobIds();
    if (ids.length) location.href = EDITOR_URL(ids);
  });
  el.prev.addEventListener('click', () => history.back());

  function addNotice(text) {
    const t = h('div', 'up-notice');
    t.appendChild(h('b', null, '!'));
    t.appendChild(h('span', null, text));
    el.notices.appendChild(t);
    while (el.notices.children.length > 3) el.notices.firstElementChild.remove();
  }

  /* --- runner: POST /jobs per file (LIVE_PARALLEL at once), then poll GET /jobs/{id} --- */
  let healthGate = null; // live: one /health check per idle->busy transition
  function enqueue(keys) {
    if (!keys.length) return;
    state.queue.push(...keys);
    if (MOCK) { pump(); return; }
    if (state.inFlight > 0) { pump(); return; }
    if (!healthGate) {
      healthGate = probeHealth().then((hs) => { healthGate = null; return hs; });
    }
    healthGate.then((hs) => {
      if (hs.state === 'down') {
        const failed = state.queue.splice(0);
        failed.forEach((key) => applyPage(key, { status: 'error', error: 'Backend unreachable: nothing was uploaded' }));
        if (failed.length) addNotice('Backend unreachable at ' + (API_BASE || location.origin) + '. Start the API, or open ?mock=1 for the demo');
        return;
      }
      pump();
    });
  }

  function pump() {
    const limit = MOCK ? 3 : LIVE_PARALLEL;
    while (state.inFlight < limit && state.queue.length) {
      const key = state.queue.shift();
      if (!state.pages.has(key)) continue;
      state.inFlight++;
      runOne(key).finally(() => { state.inFlight--; pump(); refreshFooter(); });
    }
  }

  async function runOne(key) {
    const p = state.pages.get(key);
    try {
      let zipFiles = null;
      if (!MOCK && isZip(p.file)) {
        p.els.pct.textContent = 'Unzipping…';
        const z = await unzipImages(p.file);
        if (!state.pages.has(key)) return;
        zipFiles = z.files;
        const extra = [];
        if (z.skipped) extra.push(z.skipped + (z.skipped === 1 ? ' non-image file' : ' non-image files') + ' skipped');
        if (z.locked) extra.push(z.locked + ' password-protected skipped');
        if (extra.length) addNotice(p.name + ': ' + extra.join(' · ')); // the row shows the page count when OCR is done
      }
      const { job_id } = await startPage(p.file, {
        files: zipFiles,
        fail: p.mockFail, delay: 0,
        onXhr: (xhr) => { p.xhr = xhr; },
        onProgress: (status, progress) => applyPage(key, { status, progress })
      });
      p.jobId = job_id;
      if (!MOCK) applyPage(key, { status: 'processing', progress: 100 });
      for (let i = 0; i < POLL_MAX; i++) {
        if (!state.pages.has(key)) return; // removed meanwhile
        const pg = await pollPage(job_id);
        applyPage(key, pg);
        if (pg.status === 'done' || pg.status === 'error') return;
        await wait(MOCK ? 120 : POLL_MS);
      }
      applyPage(key, { status: 'error', error: 'Timed out waiting for OCR' });
    } catch (err) {
      if (err.kind === 'cancel' || !state.pages.has(key)) return;
      applyPage(key, { status: 'error', error: err.message });
      if (err.kind === 'down') { setHealth({ state: 'down' }); addNotice(p.name + ': ' + err.message); }
      else addNotice(p.name + ': ' + err.message);
    }
  }

  /* --- connection state: the footer line tells the truth --- */
  const HEALTH_TEXT = {
    mock: '● Demo mode: mock data, runs fully offline',
    paddle: '● Connected: OCR engine ready',
    stub: '● Connected: stub OCR, text is placeholder',
    not_initialized: '● Connected: OCR engine starting',
    down: '● Backend unreachable',
    unknown: '● Connected',
    checking: '● Checking backend…'
  };
  function setHealth(hs) {
    const line = el.offline;
    const s = HEALTH_TEXT[hs.state] ? hs.state : 'unknown';
    line.dataset.state = s;
    line.textContent = HEALTH_TEXT[s];
    line.title = hs.state === 'down'
      ? 'No response from ' + (API_BASE || location.origin) + '/health. Start the API or open with ?mock=1.'
      : (hs.error ? 'OCR engine: ' + hs.error : '');
  }
  function probeHealth() {
    setHealth({ state: 'checking' });
    return realHealth().then((hs) => { setHealth(hs); return hs; });
  }

  /* --- drop zone: enter = instant swap (1 frame), leave/drop = 200ms fade --- */
  let dragDepth = 0;
  const hasFiles = (e) => e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') > -1;
  el.dz.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; el.dz.classList.add('is-drag'); });
  el.dz.addEventListener('dragover', (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
  el.dz.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) el.dz.classList.remove('is-drag'); });
  el.dz.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    el.dz.classList.remove('is-drag');
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  window.addEventListener('dragover', (e) => e.preventDefault()); // a missed drop never navigates away
  window.addEventListener('drop', (e) => e.preventDefault());
  el.dz.addEventListener('click', (e) => {
    if (e.target.closest('button, input, label')) return;
    el.input.click();
  });
  el.browse.addEventListener('click', (e) => { e.stopPropagation(); el.input.click(); });
  el.input.addEventListener('change', () => { addFiles(el.input.files); el.input.value = ''; });

  /* paste an image or PDF anywhere on the page (not while typing in a field) */
  document.addEventListener('paste', (e) => {
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    const cd = e.clipboardData;
    if (!cd) return;
    let files = Array.from(cd.files || []);
    if (!files.length) {
      files = Array.from(cd.items || []).filter((it) => it.kind === 'file').map((it) => it.getAsFile()).filter(Boolean);
    }
    if (!files.length) return;
    e.preventDefault();
    const stamp = new Date().toTimeString().slice(0, 8).replace(/:/g, '');
    addFiles(files.map((f, i) => {
      // clipboard images arrive as "image.png": give each a unique, readable name
      if (!f.name || /^image\.(png|jpe?g)$/i.test(f.name)) {
        const ext = f.type === 'image/jpeg' ? '.jpg' : '.png';
        return new File([f], 'pasted-' + stamp + (files.length > 1 ? '-' + (i + 1) : '') + ext, { type: f.type || 'image/png' });
      }
      return f;
    }));
  });

  /* "Try a sample page" */
  el.sample.addEventListener('click', (e) => {
    e.stopPropagation();
    if (MOCK) {
      addFiles([new File([new Uint8Array(186000)], SAMPLE_NAME, { type: 'image/png' })]);
      return;
    }
    el.sample.disabled = true;
    fetch(SAMPLE_URL, { cache: 'no-store' })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then((b) => addFiles([new File([b], SAMPLE_NAME, { type: b.type || 'image/png' })]))
      .catch(() => addNotice('Sample page is not available on this server'))
      .finally(() => { el.sample.disabled = false; });
  });
  if (!MOCK) {
    /* GET, not HEAD: the FastAPI ui_sub_file route only implements GET and
       answers HEAD with 405, which would hide the button even though the
       sample exists. The body is tiny and unused - presence is all we need. */
    fetch(SAMPLE_URL, { cache: 'no-store' })
      .then((r) => { el.sample.hidden = !r.ok; })
      .catch(() => { el.sample.hidden = true; });
  }

  refreshFooter();
  if (MOCK) setHealth({ state: 'mock' }); else probeHealth();

  /* Watch-demo modal (markup in index.html): opens from the empty-state
     button; closes on backdrop, the X, or Esc. Pauses on close. */
  const demoModal = document.getElementById('demo-modal');
  if (demoModal) {
    const demoVideo = demoModal.querySelector('video');
    const demoClose = demoModal.querySelector('.demo-modal-close');
    const openDemo = () => {
      demoModal.hidden = false;
      document.body.classList.add('demo-open');
      if (demoVideo) { demoVideo.currentTime = 0; demoVideo.play().catch(() => {}); }
      if (demoClose) demoClose.focus();
    };
    const closeDemo = () => {
      if (demoModal.hidden) return;
      if (demoVideo) demoVideo.pause();
      demoModal.hidden = true;
      document.body.classList.remove('demo-open');
      el.demo.focus();
    };
    el.demo.addEventListener('click', openDemo);
    demoModal.addEventListener('click', (e) => { if (e.target.closest('[data-demo-close]')) closeDemo(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDemo(); });
  }

  /* ?demo : scripted drag-over -> drop on the mock engine (generic sample names). */
  if (DEMO) {
    setTimeout(() => el.dz.classList.add('is-drag'), 600);
    setTimeout(() => {
      el.dz.classList.remove('is-drag');
      addFiles([
        new File([new Uint8Array(412000)], 'thirukkural-chapter-01.pdf', { type: 'application/pdf' }),
        new File([new Uint8Array(238000)], 'page-014.jpg', { type: 'image/jpeg' })
      ]);
    }, 3400);
  }
})();
