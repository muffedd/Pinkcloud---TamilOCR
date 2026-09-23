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
                           404 -> {"detail": "job not found"} */

var MOCK_DEFAULT = false;
var QS = new URLSearchParams(location.search);
var USE_MOCK = QS.has("mock") ? QS.get("mock") !== "0" : MOCK_DEFAULT;
var API_BASE = (QS.get("api") || "").replace(/\/+$/, "");

var GET_JOB = function (id) { return API_BASE + "/jobs/" + encodeURIComponent(id); };

/* Scan-image route: the backend owner is adding a safe route that serves the
   stored scan image for a job page. The exact URL is TBD - update this ONE
   constant when the route lands in schema/endpoints.md. Until then the editor
   loads it best-effort and falls back to its placeholder paper on any error. */
/* TODO(backend): confirm the scan-image route and update SCAN_IMAGE. */
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

/* Corrections save-back: schema/endpoints.md defines NO endpoint for
   persisting reviewer corrections yet, so edits stay client-side.
   TODO(backend): once a corrections endpoint lands in schema/endpoints.md,
   implement this against it and have the editor call it after each accepted
   fix. Do not invent a route before the contract names one. */
function saveCorrections(/* jobId, page, corrections */) {
  return Promise.reject(ApiError(
    "Corrections save-back is not implemented: no endpoint in schema/endpoints.md yet",
    "http"
  ));
}

/* Best-effort URL for the scan image behind a page (see SCAN_IMAGE above). */
function pageImageUrl(jobId, page) {
  return SCAN_IMAGE(jobId, page);
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
  saveCorrections: saveCorrections
};
