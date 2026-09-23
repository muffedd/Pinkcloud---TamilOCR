# Pink Cloud - Tamil OCR

**Tamil OCR for damaged heritage scans, with a human review loop driven by confidence.**

Palm-leaf manuscripts and old Tamil print break ordinary OCR. The ink is faded, the pages are stained or warped, and the letterforms are older than the ones modern models were trained on. Pink Cloud accepts that the machine will be wrong sometimes. It scores every word, shows a reviewer only the words it doubts, and makes each fix a single keystroke. The result is Unicode Tamil text and a searchable PDF you can trust, without proofreading every line by hand.

> **Status:** the upload, review, library and export pages all run against the FastAPI backend on `main`, and the upload, editor and library pages also run fully offline in demo mode (`?mock=1`). The backend runs upload → quality routing → Tamil OCR (Sarvam Document AI by default, local PaddleOCR as the fallback), serves page images, saves reviewer corrections, and exports a searchable PDF, TXT and DOCX with a processing receipt. See [Current state](#current-state).

## How it works

The website has three steps. The stepper at the top of the upload screen shows them (the editor's sidebar splits the last one into Review and Export). Before you start, scan or photograph the palm leaf or printed page; PDF, JPG, PNG or TIFF all work, and multi-page PDFs and TIFFs are fine.

1. **Upload scans** - drag and drop or browse PDF, JPG, PNG or TIFF files, or a ZIP of page images (JPG, PNG, TIFF, WEBP; up to 100). A ZIP stays one row and one job, with one page per image in file-name order (page2 before page10); PDFs and other files inside it are skipped. Each file becomes a job, and its row shows status live: queued → uploading → processing → done.
2. **Tamil OCR** - every page is scored on blur, contrast, noise and skew, then badged **FAST** (clean) or **HEAVY** (damaged, flagged for review). Sarvam Document AI (`ta-IN`) reads each page by default; without a Sarvam key, or when a call fails, a local PaddleOCR PP-OCRv5 pass with the Tamil recognition model runs instead. Each line gets a bounding box and a confidence score.
3. **Review & export** - the editor opens with the doubtful words queued. Fix them, then press **Export**: pending fixes are saved and the export page opens with the downloads.

Every job also shows up in the **Library** (the Documents link in the sidebar), where you can reopen it or search its text.

## The review editor

The editor sits in a sidebar shell (Workflow: Upload, Tamil OCR, Review, Export, plus a Library link) and puts three panes side by side: the scan, the Unicode Tamil text, and the review queue.

- **Every word is boxed.** Boxes are drawn on the scan at the OCR's coordinates. Click a word on the scan or in the text and the other pane highlights it too.
- **Doubtful words are flagged.** Each word falls into a confidence band: Auto (≥ 0.95), OK (0.80 - 0.95), or Doubt (< 0.80). On pages the backend routed to review (`needs_review` or HEAVY), the Doubt floor rises to 0.95, so any word that is not clean is queued. **Auto** mode queues only Doubt words. **Review** mode adds the OK words for a stricter pass. The queue header counts how many words were routed to you and how many you have fixed. A heatmap view shows the weak areas of the scan at a glance.
- **The queue shows the scan.** Each queue card carries a crop of its line from the page image, so you can read the original without looking away. When a page has suggested replacements, they are listed on the card and in the fix box, and number keys `1` - `9` pick one. The shape is in [`schema/suggestions-contract.md`](schema/suggestions-contract.md); it is a proposal, the editor reads it from mock data today, and pages without it work as before.
- **Fixes take one key.** `J` / `K` move through the queue, `Enter` opens the fix box, and `Enter` again accepts. `Esc` rejects. You can type the correction in Tanglish (for example `vaalarivan` → வாலறிவன்) using the built-in offline transliterator. No Tamil keyboard needed.
- **Fixes are saved.** Accepted fixes are saved to the server (`GET` / `PUT /jobs/{id}/corrections`, see [`schema/corrections-endpoint.md`](schema/corrections-endpoint.md)) and kept across pages. If the backend cannot be reached they stay in the browser until it can. The editor shows one page at a time (`?page=<n>`); the prev / next buttons move between pages of a multi-page job.
- **Every change is traceable.** The Provenance lens colours each word by where it came from: Raw OCR, Rule, Swap, LLM or Human. Accepted fixes are marked Human.
- **Export from the editor.** The Export button opens a dialog that saves pending fixes, then opens `export.html?job=<id>`. If some fixes are only saved in this browser, the dialog says the PDF will not include them and offers the corrected text instead.

## The library

`library.html` lists every job, newest first, with its status and page count (`GET /jobs`). The search box searches the OCR text of finished jobs (`GET /search`) and shows each hit with its line highlighted. The query is kept in the URL as `?q=`, so a search survives a reload or a Back. A hit opens the editor on that job and page (`editor.html?job=<id>&page=<n>`).

## The export page

`export.html?job=<id>` shows the job's processing receipt as a card (`GET /jobs/{id}/receipt`) with **Download PDF** (the scan with the Tamil text layer under it) and **Download TXT**. DOCX comes from the backend; Markdown, CSV and XML are built in the browser from the job and its saved corrections. The page has its own loading, job-not-found and not-ready states, and waits for a job that is still processing. `?reviewer=<name>` is written into the receipt and the exports.

## Current state

| Area | State |
| --- | --- |
| Upload screen (`index.html`) | Complete. Live mode posts to the backend; `?mock=1` runs offline. |
| Review editor (`editor.html`) | Complete. Live mode loads a job by `?job=<job_id>` (and `&page=<n>`); `?mock=1` loads the Thirukkural demo page, `?mock=1&fixture=sample1` or `sample2` loads real Sarvam output on its real scan. |
| Library (`library.html`) | Complete. Live mode reads `GET /jobs` and `GET /search`; `?mock=1` runs offline. The Fixed column shows "-" until the backend sends a corrections count. |
| Export page (`export.html`) | Complete. Live only: needs a finished job. |
| Backend on `main` | Upload (single file or several images as one job), SHA-256 master storage, FAST/HEAVY routing, OCR, contract JSON; job list and full-text search; page images; corrections save-back; receipt and PDF / TXT / DOCX export; the UI served from the API origin so live mode needs no CORS. |
| Not built yet | The suggestion source behind `suggestions[]` (the editor uses mock suggestions). A repair pass and heavier OCR for HEAVY pages inside the app. |

The live path works end to end. The offline demo still covers upload, review and the library without a backend.

## Run it

### 1. Frontend demo (offline, no backend)

From the repo root:

```bash
python -m http.server 8877
```

Then open:

- http://127.0.0.1:8877/index.html?mock=1 - upload screen. Drop a few files and they run through mock OCR.
- http://127.0.0.1:8877/editor.html?mock=1 - review editor on the demo page. Press `J`, then `Enter`, then accept the fix. Add `&fixture=sample1` (or `sample2`) for real Sarvam output on a real scan.
- http://127.0.0.1:8877/library.html?mock=1 - library with mock jobs and search.

No build step and no network access. Fonts are bundled in `fonts/`.

### 2. Backend (FastAPI + SQLite)

Python 3.11 to 3.13, CPU only. Works on Windows with no Docker, poppler or torch.

```bash
cd fastapi/sqlite
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- http://127.0.0.1:8000/health → `{"ok": true, "ocr_engine": "..."}`
- http://127.0.0.1:8000/docs → Swagger UI

OCR runs on Sarvam Document AI by default. Set `SARVAM_API_KEY` in the server's environment (never commit it). Set `OCR_ENGINE=paddle` to stay offline.

The local PaddleOCR engine, used for `OCR_ENGINE=paddle` and as the Sarvam fallback, is optional and a large download:

```bash
pip install "paddlepaddle==3.3.1" "paddleocr==3.7.0"
```

With no Sarvam key and no usable paddle (or a CPU without AVX), the API returns marked `[stub]` lines in the same contract shape, and `/health` reports `"ocr_engine": "stub"`. Check `/health` before trusting OCR text. All engine variables are in [`fastapi/sqlite/RUN.md`](fastapi/sqlite/RUN.md).

### 3. Quick test

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/jobs -F "file=@scan.png"   # {"job_id": "..."}
curl -s http://127.0.0.1:8000/jobs/<job_id>                        # status + result JSON
curl -s "http://127.0.0.1:8000/search?q=<word>"                    # full-text search
```

Accepted types: pdf, jpg, jpeg, png, tiff, webp. Anything else returns HTTP 400. Repeat `-F "files=@p1.jpg" -F "files=@p2.png"` to send several images as one job, one page per image. Processing is synchronous, so the job is already `done` or `error` when `POST /jobs` returns.

### 4. Live frontend against the backend

The backend serves the UI itself, so open http://127.0.0.1:8000/ and everything runs on one origin with no CORS. `editor.html?job=<job_id>`, `library.html` and `export.html?job=<job_id>` work the same way. If you serve the UI from somewhere else, add `?api=http://127.0.0.1:8000` (the backend does not send CORS headers).

### 5. Tests

```bash
cd fastapi/sqlite
python -m pytest tests/ -v      # the real-OCR end-to-end test skips when paddle is unusable
```

```bash
bash scripts/smoke.sh           # static checks on the frontend files and demo data
```

Full backend notes, including threshold tuning on sample scans, are in [`fastapi/sqlite/RUN.md`](fastapi/sqlite/RUN.md).

## Backend layout

The backend lives in `fastapi/sqlite/app/`.

| File | Role |
| --- | --- |
| `app/main.py` | FastAPI endpoints: `GET /health`, `POST /jobs`, `GET /jobs`, `GET /jobs/{job_id}`, `GET /search`, `GET /jobs/{job_id}/pages/{n}/image`, `GET` / `PUT /jobs/{job_id}/corrections`, `GET /jobs/{job_id}/receipt`, `/export.pdf`, `/export.txt`, `/export.docx`; also serves the UI files |
| `app/db.py` | SQLite jobs table plus an FTS5 index over OCR lines (`init_db`, `create_job`, `get_job`, `list_jobs`, `set_result`, `search_lines`) |
| `app/storage.py` | Byte-for-byte masters: SHA-256 hashed before write, verified after |
| `app/router.py` | Per-page quality metrics (blur, contrast, noise, skew_deg) → **FAST** / **HEAVY** badge |
| `app/pdfutil.py` | PDF via pypdfium2, images via cv2; long side capped at 1600 px |
| `app/ocr.py` | Sarvam Document AI by default; lazy PaddleOCR fallback (PP-OCRv5_mobile_det + ta_PP-OCRv5_mobile_rec); stub result if neither is usable |
| `app/export.py` | Applies saved corrections, builds the receipt and the PDF / TXT / DOCX exports |
| `app/schema_out.py` | Builds the frozen output contract JSON (`schema/schema.json`) |

The output contract is in [`schema/schema.json`](schema/schema.json), the endpoint reference in [`schema/endpoints.md`](schema/endpoints.md), the corrections route in [`schema/corrections-endpoint.md`](schema/corrections-endpoint.md), the proposed suggestions field in [`schema/suggestions-contract.md`](schema/suggestions-contract.md), and a sample page in [`schema/doc_demo.json`](schema/doc_demo.json).

## Frontend layout

| File | Role |
| --- | --- |
| `index.html`, `upload.js`, `upload.css` | Upload screen and stepper |
| `sidebar.css` | Upload page sidebar: Pink Cloud chip, Documents (opens the Library, keeps `?mock` / `?api`), Scans (soon). Page content sits in one inset panel; the sidebar hides below 900px |
| `intro.js`, `intro.css` | Landing intro on `index.html` (from `design-drafts/pinkcloud-intro.html`): plays once per browser session, click/Esc/Skip jumps to the slide-up, skipped for reduced motion. Variants: `logo` (default, the `logo@2x.svg` cloud mark + "Pinkcloud" wordmark in dot-matrix), `pinkcloud` (bolt + "Pink Cloud"), `omni` (bolt + "Omni"). `?intro=0` off, `?intro=1` replay, `?intro=<variant>` plays and remembers that variant in the browser; any `?intro=` shows a variant picker on the sheet. Preview: `design-drafts/intro-variants.html` |
| `library.html`, `library.js`, `library.css` | Library: job list (`GET /jobs`) and text search (`GET /search`, `?q=` kept in the URL); a hit opens `editor.html?job=<id>&page=<n>` |
| `export.html`, `export.js`, `export.css` | Export page: receipt card, PDF/TXT downloads, plus DOCX (backend) and Markdown / CSV / XML (built in the browser); loading, missing and not-ready states |
| `nav-back.js` | Back button on Library and Export: returns to the previous Pink Cloud page, else to Upload (Library) or the job's editor (Export) |
| `editor.html`, `editor.js`, `editor.css` | Review editor: sidebar shell, routed review queue with scan crops and suggestions, corrections save-back, page pager, export dialog |
| `samples/editor/` | Offline editor fixtures from real Sarvam output (`make_fixtures.py`); fake suggestions |
| `api.js` | Data access: live backend or `?mock=1` demo |
| `translit.js` | Offline Tanglish → Tamil transliteration for fixes |
| `tokens.css`, `ui.css` | Design tokens and shared components. Motion: `--pc-ease-bounce` / `--pc-ease-overshoot` (from `design-drafts/icon-motion-*`); solid and outline buttons lift 1px on hover and spring back after a press. Icon motion (from `design-drafts/icon-motion-a.html` spring + `icon-motion-b.html` draw-on): Library and Export status dots pop in and Processing dots pulse (A), spinners turn in springy quarter steps (A), Export not-found/failed icons pop in (A), Export download icons and a finished upload row's action icons draw on (B). Nothing in a sidebar or the intro moves. Reduced motion keeps color changes only |
| `tokens.css` sizing scale | Generic steps for new work: spacing `--pc-space-0/1/2/3/4/5/6/8/10/12/16` (0-64px), font size `--pc-fs-xs`..`--pc-fs-4xl` (11-24px), line height `--pc-lh-*`, weight `--pc-fw-*`, tracking `--pc-track-*`, icons `--pc-icon-xs`..`xl` (12-24px), control heights `--pc-control-sm/md/lg` (30/36/44), radius `--pc-radius-xs`..`xl` (3-16px), containers `--pc-container-sm`..`xl` (640-1200px), stacking `--pc-z-*`, breakpoints `--pc-bp-*` (reference only). Role tokens (`--pc-fs-btn`, `--pc-btn-h`, ...) are unchanged |

## Design system

| Folder | Contents |
| --- | --- |
| `design system/foundations/` | Color systems, typography specimen, design brief, and shared styles |
| `design system/components/` | Component sheet, library, and standalone button/toggle examples |
| `design system/layout/` | Three-page layout plan and annotated editor mock |
| `design-drafts/` | Drafts, not wired into the app except where noted. Loaders: `loader-variant-a.html` (smoke), `-b`, `-c` (shader), `pink-cloud-loading-shader-D.html` (3D glyph swarm). Intros: `pinkcloud-intro.html` (source of the landing intro), `omni-intro-variant-b.html` (lightning strike), `omni-intro-variant-c.html` (particles), `intro-variants.html` (preview of the shipped intro variants). Icon motion: `icon-motion-a.html` (spring scale), `-b` (draw-on), `-c` (interaction), source of the motion tokens |

Open the HTML design files in a browser. No build step is needed.

## Where it goes next

- Send HEAVY pages through image repair and a heavier OCR pass.
- Build a real suggestion source and emit `suggestions[]` from the backend (add it to `schema/schema.json` in the same change).
- Use saved corrections to improve later passes.
- Keep the offline PaddleOCR route working alongside the hosted Sarvam default.

## Heavy repair and OCR provider evaluation

The experimental CPU repair pipeline and OCR comparisons are in `repair/`, `scripts/text_filter.py`, and `ocr_outputs/`. This evaluation came before the app switched engines: the backend now uses Sarvam Document AI by default with PaddleOCR as the fallback (see [`fastapi/sqlite/RUN.md`](fastapi/sqlite/RUN.md)), but the repair pipeline is still not part of the app. `tools/sarvam_ocr.py` is a standalone CLI that runs one file through Sarvam and saves the output.

`repair/repair.py` now routes per page using input ink contrast (threshold `0.25`, matching the repair pipeline's fadedness threshold) and paper brightness (90th percentile threshold `240`). Clean pages use the raw image; damaged pages use the repaired grayscale `_g.png`. The binary PNG is for display only and must not be sent to OCR.

| Page | Gate decision | Ink contrast | Background p90 | Selected OCR image |
| --- | --- | ---: | ---: | --- |
| sample1 | CLEAN | 0.773 | 255.0 | `raw/sample1.png` |
| sample2 | DAMAGED | 0.518 | 138.0 | `repair/out/sample2_g.png` |

### OCR evaluation highlights

CER is measured against `gt_cict_narrinai_p3.txt`, which matches sample1 only. Tamil ratio and garbage rate are fractions; confidence is the mean layout-box score, not recognition certainty. Segmentation differs between OCR providers.

| Page / input | OCR path | Tamil ratio | Garbage rate | Repeat-loop lines | Mean confidence | CER | API latency |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| sample1 raw | PaddleOCR-VL-1.6 | 0.845 | 0.060 | 0 | 0.717 | 33.87% | Not recorded |
| sample1 raw | Sarvam Document AI | 0.978 | 0.000 | 0 | 0.742 | **1.54%** | 12.54 s |
| sample1 raw + filter | PaddleOCR-VL-1.6 | 0.979 | 0.001 | 0 | 0.712 | 27.22% | — |
| sample2 raw | PaddleOCR-VL-1.6 | 0.920 | 0.055 | 1 | 0.808 | N/A | Not recorded |
| sample2 raw | Sarvam Document AI | 0.920 | 0.000 | 0 | 0.759 | N/A | 9.51 s |
| sample2 repaired grayscale | PaddleOCR-VL-1.6 | 0.974 | 0.007 | 0 | 0.744 | N/A | 267.34 s POST; ~305.47 s to saved result |
| sample2 repaired grayscale | Sarvam Document AI | 0.986 | 0.000 | 0 | 0.798 | N/A | 17.03 s |

Sarvam sample2 raw includes a false English “no legible text” preamble despite transcribing Tamil; its zero garbage rate does not capture this semantic error. Sample2 lacks matching ground truth, so no CER winner is established. These small tests suggest Sarvam performed better on the measured sample1 CER, but are not a general accuracy guarantee.

Detailed tables, OCR artifacts, and runnable checks: [`OCR_API_COMPARISON.md`](OCR_API_COMPARISON.md), [`OCR_TEST_SCORES.md`](OCR_TEST_SCORES.md), and [`OCR_PROGRESS_PROOF.md`](OCR_PROGRESS_PROOF.md). API credentials belong in deployment secrets, never in Git; the Sarvam API key is not part of this repository.

## License

MIT. See [`LICENSE`](LICENSE).
