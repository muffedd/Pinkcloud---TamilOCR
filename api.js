/* Pink Cloud data access.
   LIVE is the default, same seam as upload.js (slice 3):
     editor.html?job=<job_id>   -> GET /jobs/{job_id} on the live backend
     editor.html?mock=1         -> the offline Kural demo (schema/doc_demo.json),
                                   unchanged
   MOCK_DEFAULT = true flips the demo on without the query param.
   API origin: same origin as the page by default. For a backend on another
   origin (needs CORS on the backend) use ?api=http://127.0.0.1:8000

   Contract: schema/endpoints.md (fastapi/sqlite, app 0.2.0).
     GET /health        -> {ok, ocr_engine, ocr_error?}
     GET /jobs/{job_id} -> {job_id, filename, sha256, status, created_at, result}
                           status: pending | done | error
                           result: {pages:[...]} | {error} | null
                           404 -> {"detail": "job not found"}
   Plus, live on main:
     GET /jobs/{job_id}/pages/{n}/image  -> per-page scan PNG (bbox space)
     GET|PUT /jobs/{job_id}/corrections  -> reviewer fix map
                                            (schema/corrections-endpoint.md) */

var MOCK_DEFAULT = false;
var QS = new URLSearchParams(location.search);
var USE_MOCK = QS.has("mock") ? QS.get("mock") !== "0" : MOCK_DEFAULT;
var API_BASE = (QS.get("api") || "").replace(/\/+$/, "");

var GET_JOB = function (id) { return API_BASE + "/jobs/" + encodeURIComponent(id); };

/* Scan-image route: GET /jobs/{job_id}/pages/{n}/image serves page n
   (1-based) of a finished job as a PNG rendered in the OCR bbox coordinate
   space - draw bboxes directly on it. The editor loads it best-effort and
   falls back to its placeholder paper on any error (404 = unknown/unfinished
   job or out-of-range page). */
var SCAN_IMAGE = function (id, page) {
  return API_BASE + "/jobs/" + encodeURIComponent(id) + "/pages/" + page + "/image";
};

/* Poll pacing for a job that is still "pending" (same shape as upload.js:
   POST /jobs is synchronous today, so pending is rare - but the contract
   allows it if processing moves to a background task). */
var POLL_MS = 1000;
var POLL_MAX = 240; /* depth cap ~4 min per job */

function ApiError(message, kind) {
  var err = new Error(message);
  err.kind = kind; /* "down" = backend unreachable | "http" = server answered with an error */
  return err;
}

function detailText(body) {
  if (!body || body.detail == null) return "";
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map(function (d) { return (d && d.msg) || ""; }).filter(Boolean).join("; ");
  }
  return String(body.detail);
}

/* GET /jobs/{job_id} -> the job object. Throws ApiError:
     kind "down" - no response at all (backend not running / CORS)
     kind "http" - answered with an error (404 = job not found) */
function getJob(jobId) {
  return fetch(GET_JOB(jobId), { cache: "no-store" })
    .catch(function () { throw ApiError("Backend unreachable (GET /jobs/" + jobId + ")", "down"); })
    .then(function (res) {
      if (res.status === 404) throw ApiError("Job not found on the server (" + jobId + ")", "http");
      if (!res.ok) {
        return res.json().catch(function () { return null; }).then(function (body) {
          var d = detailText(body);
          throw ApiError((d || "GET /jobs failed") + " (" + res.status + ")", "http");
        });
      }
      return res.json();
    });
}

var CORRECTIONS = function (id) { return API_BASE + "/jobs/" + encodeURIComponent(id) + "/corrections"; };

/* Attach the HTTP status to an ApiError so callers can split 404 (route not
   deployed on an older backend -> silent local fallback) from a real server
   error like 500 (surface it, never a silent fallback). */
function httpError(message, status) {
  var err = ApiError(message, "http");
  err.status = status;
  return err;
}

/* GET /jobs/{job_id}/corrections -> {job_id, corrections, updated_at}
   (corrections: [] and updated_at: null when none saved yet).
   Throws ApiError kind "down" (network) | "http" with .status.
   Contract: schema/corrections-endpoint.md. */
function getCorrections(jobId) {
  return fetch(CORRECTIONS(jobId), { cache: "no-store" })
    .catch(function () { throw ApiError("Backend unreachable (GET corrections)", "down"); })
    .then(function (res) {
      if (!res.ok) throw httpError("GET corrections -> " + res.status, res.status);
      return res.json();
    });
}

/* PUT /jobs/{job_id}/corrections - REPLACES the job's full correction map.
   corrections: [{page:int, line:str, word:int, before:str, after:str}, ...],
   soft cap 10k per job. An empty array clears the job's corrections.
   Same error split as getCorrections. */
function saveCorrections(jobId, corrections) {
  return fetch(CORRECTIONS(jobId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ corrections: corrections })
  })
    .catch(function () { throw ApiError("Backend unreachable (PUT corrections)", "down"); })
    .then(function (res) {
      if (!res.ok) throw httpError("PUT corrections -> " + res.status, res.status);
      return res.json();
    });
}

/* Library list + search (library.html). Live on main (feat/jobs-search):
     GET /jobs?limit=50&offset=0   (limit 1..200) -> {total, limit, offset,
         jobs: [{job_id, filename, sha256, status, created_at, page_count,
                 pages_needing_review, error, result_url, receipt_url,
                 corrections_count?}]}  newest first; page_count /
         pages_needing_review / receipt_url are null unless status is done.
         corrections_count is being added on the backend side; the Library
         shows "-" until it arrives.
     GET /search?q=<1..200 chars>&limit=20&offset=0  (limit 1..100) ->
         {query, total, limit, offset,
          results: [{job_id, filename, page, line, snippet, score}]}
         snippet wraps hits in <mark>...</mark> (raw text otherwise - render
         it as text, never as HTML); lower score = better match.
         Blank q -> 400, out-of-range limits -> 422, no FTS5 -> 503.
   Both throw ApiError kind "down" (no response) | "missing" (404/405: this
   backend predates the route) | "http" (any other error, with .status). */
var LIST_JOBS = function (limit, offset) {
  return API_BASE + "/jobs?limit=" + limit + "&offset=" + offset;
};
var SEARCH = function (q, limit, offset) {
  return API_BASE + "/search?q=" + encodeURIComponent(q) + "&limit=" + limit + "&offset=" + offset;
};

function getListJson(url, label) {
  return fetch(url, { cache: "no-store" })
    .catch(function () { throw ApiError("Backend unreachable (" + label + ")", "down"); })
    .then(function (res) {
      if (res.ok) return res.json();
      return res.json().catch(function () { return null; }).then(function (body) {
        var d = detailText(body);
        var err = httpError((d || label + " failed") + " (" + res.status + ")", res.status);
        if (res.status === 404 || res.status === 405) err.kind = "missing";
        throw err;
      });
    });
}

/* GET /jobs -> {total, limit, offset, jobs} */
function listJobs(limit, offset) {
  return getListJson(LIST_JOBS(limit || 50, offset || 0), "GET /jobs");
}

/* GET /search -> {query, total, limit, offset, results} */
function searchJobs(q, limit, offset) {
  return getListJson(SEARCH(q, limit || 20, offset || 0), "GET /search");
}

/* Best-effort URL for the scan image behind a page (see SCAN_IMAGE above). */
function pageImageUrl(jobId, page) {
  return SCAN_IMAGE(jobId, page);
}

/* ---------------- AI fix + learned-fix dictionary (V4) ----------------
   Shapes: schema/ai-fix-contract.md (shipped backend contract).

   POST /jobs/{job_id}/suggest  {page, line, word, before, context}
     -> {candidates: [{text, score?, source: "llm"}]}   (503 = AI unavailable)
   The backend route exists (app/suggest.py) but answers 503 until
   GEMINI_API_KEY is set on the server, so suggestWord() answers from a local
   stub. Flip to the real endpoint with ONE line, after the key is set and a
   real route test passes: */
var AI_SUGGEST_LIVE = true;
var SUGGEST = function (id) { return API_BASE + "/jobs/" + encodeURIComponent(id) + "/suggest"; };

function realSuggest(jobId, req) {
  return fetch(SUGGEST(jobId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req)
  })
    .catch(function () { throw ApiError("Backend unreachable (POST suggest)", "down"); })
    .then(function (res) {
      if (!res.ok) throw httpError("POST suggest -> " + res.status, res.status);
      return res.json();
    });
}

/* Stub: known demo words map to their reading; otherwise stray vowel signs /
   viramas at the start of a word and edge punctuation are dropped. No
   change -> no candidates. ?aifail=1 makes it answer 503 (degradation test). */
var MOCK_AI = { "வாழறிவன்": "வாலறிவன்", "ழுதல": "முதல", "மென்றோள்": "மென்தோள்" };
function stubSuggest(jobId, req) {
  return new Promise(function (resolve, reject) {
    setTimeout(function () {
      if (QS.get("aifail") === "1") { reject(httpError("POST suggest -> 503", 503)); return; }
      var before = String(req.before || "");
      var text = MOCK_AI[before] ||
        before.replace(/^[\u0BBE-\u0BCD\u0BD7]+/, "").replace(/^[^\u0B80-\u0BFFA-Za-z0-9]+|[^\u0B80-\u0BFFA-Za-z0-9]+$/g, "");
      resolve({ candidates: text && text !== before ? [{ text: text, score: 0.88, source: "llm" }] : [] });
    }, 900);
  });
}

function suggestWord(jobId, req) {
  return AI_SUGGEST_LIVE && jobId && !USE_MOCK ? realSuggest(jobId, req) : stubSuggest(jobId, req);
}

/* GET /dictionary -> learned OCR word -> fix pairs, across jobs. Accepts
   {entries: {before: after}} or {entries: [{before, after}]}; returns a plain
   {before: after} map. Throws ApiError (.status on HTTP errors). */
var DICTIONARY = API_BASE + "/dictionary";
function getDictionary() {
  return fetch(DICTIONARY, { cache: "no-store" })
    .catch(function () { throw ApiError("Backend unreachable (GET /dictionary)", "down"); })
    .then(function (res) {
      if (!res.ok) throw httpError("GET /dictionary -> " + res.status, res.status);
      return res.json();
    })
    .then(function (body) {
      var e = body && (body.entries || body.dictionary || body);
      var out = {};
      if (Array.isArray(e)) {
        e.forEach(function (x) { if (x && x.before && x.after) out[String(x.before)] = String(x.after); });
      } else if (e && typeof e === "object") {
        Object.keys(e).forEach(function (k) { if (typeof e[k] === "string" && e[k]) out[k] = e[k]; });
      }
      return out;
    });
}

/* POST /dictionary/skip {job_id, page, line, word, before, after}: the
   reviewer undid a learned fix here. Fire-and-forget: never throws. */
function skipDictionary(entry) {
  try {
    return fetch(DICTIONARY + "/skip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
      keepalive: true
    }).catch(function () { return null; });
  } catch (e) { return Promise.resolve(null); }
}

/* Mock path, untouched: the Kural demo page from schema/doc_demo.json.
   In live mode the editor loads a whole job via getJob() instead; getPage(n)
   only exists for the demo. */
async function getPage(n) {
  if (USE_MOCK) {
    const res = await fetch("./schema/doc_demo.json", { cache: "no-store" });
    if (!res.ok) {
      throw new Error("mock page " + n + " failed to load (" + res.status + ")");
    }
    return res.json();
  }
  throw ApiError("getPage(n) is mock-only; in live mode load a job with getJob(jobId)", "http");
}

window.PC_API = {
  USE_MOCK: USE_MOCK,
  API_BASE: API_BASE,
  POLL_MS: POLL_MS,
  POLL_MAX: POLL_MAX,
  getPage: getPage,
  getJob: getJob,
  pageImageUrl: pageImageUrl,
  getCorrections: getCorrections,
  saveCorrections: saveCorrections,
  listJobs: listJobs,
  searchJobs: searchJobs,
  suggestWord: suggestWord,
  getDictionary: getDictionary,
  skipDictionary: skipDictionary
};
