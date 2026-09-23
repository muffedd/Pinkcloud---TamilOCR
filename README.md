# Pink Cloud - Tamil OCR

**Tamil OCR for damaged heritage scans, with a human review loop driven by confidence.**

Palm-leaf manuscripts and old Tamil print break ordinary OCR. The ink is faded, the pages are stained or warped, and the letterforms are older than the ones modern models were trained on. Pink Cloud accepts that the machine will be wrong sometimes. It scores every word, shows a reviewer only the words it doubts, and makes each fix a single keystroke. The result is Unicode Tamil text and a searchable PDF you can trust, without proofreading every line by hand.

> **Status:** the frontend is complete and runs fully offline in demo mode (`?mock=1`). The FastAPI backend runs upload → quality routing → Tamil OCR today. The scan-image and PDF-export routes are on branches and are being merged into `main`. See [Current state](#current-state).

## How it works

The app follows five steps. The stepper at the top of the upload screen shows them.

1. **Scan pages** - photograph or scan the palm leaf or printed page.
2. **Prepare files** - gather the scans as PDF, JPG, PNG or TIFF. Multi-page PDFs and TIFFs are fine.
3. **Upload** - drag and drop or browse. Each file becomes a job, and its row shows status live: queued → uploading → processing → done.
4. **Tamil OCR** - every page is scored on blur, contrast, noise and skew, then badged **FAST** (clean) or **HEAVY** (damaged, sent to repair). A PaddleOCR PP-OCRv5 pass with the Tamil recognition model reads each line and gives it a bounding box and a confidence score.
5. **Review & export PDF** - the editor opens with the doubtful words queued. Fix them, then export.

## The review editor

The editor puts three panes side by side: the scan, the Unicode Tamil text, and the review queue.

- **Every word is boxed.** Boxes are drawn on the scan at the OCR's coordinates. Click a word on the scan or in the text and the other pane highlights it too.
- **Doubtful words are flagged.** Each word falls into a confidence band: Auto (≥ 0.90), OK, or Doubt (< 0.75). **Auto** mode queues only Doubt words. **Review** mode adds the OK words for a stricter pass. A heatmap view shows the weak areas of the scan at a glance.
- **Fixes take one key.** `J` / `K` move through the queue, `Enter` opens the fix box, and `Enter` again accepts. `Esc` rejects. You can type the correction in Tanglish (for example `vaalarivan` → வாலறிவன்) using the built-in offline transliterator. No Tamil keyboard needed.
- **Every change is traceable.** The Provenance lens colours each word by where it came from: Raw OCR, Rule, Swap, LLM or Human. Accepted fixes are marked Human.
- **Export a searchable PDF.** The finished job exports as a PDF with the Tamil text layer under the scan.

## Current state

| Area | State |
| --- | --- |
| Upload screen (`index.html`) | Complete. Live mode posts to the backend; `?mock=1` runs offline. |
| Review editor (`editor.html`) | Complete. Live mode loads a job by `?job=<job_id>`; `?mock=1` loads the Thirukkural demo page. |
| Backend on `main` | `GET /health`, `POST /jobs`, `GET /jobs/{job_id}`: upload, SHA-256 master storage, FAST/HEAVY routing, OCR, contract JSON. |
| Backend routes being merged | Page scan images (`slice/per-page-image`), `export.pdf` / `export.txt` / receipt (`slice/pipe-export-receipt`), and serving the UI from the API origin so live mode needs no CORS (`slice/b01-static-serving`). |
| Not built yet | Saving reviewer corrections back to the server. Edits stay in the browser for now. |

For judging today, the offline demo is the complete experience. The live path works end to end once the three branches above land.

## Run it

### 1. Frontend demo (offline, no backend)

From the repo root:

```bash
python -m http.server 8877
```

Then open:

- http://127.0.0.1:8877/index.html?mock=1 - upload screen. Drop a few files and they run through mock OCR.
- http://127.0.0.1:8877/editor.html?mock=1 - review editor on the demo page. Press `J`, then `Enter`, then accept the fix.

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

Real OCR is optional and a large download:

```bash
pip install "paddlepaddle==3.3.1" "paddleocr==3.7.0"
```

Without it, or on a CPU without AVX, the API returns marked `[stub]` lines in the same contract shape, and `/health` reports `"ocr_engine": "stub"`. Check `/health` before trusting OCR text.

### 3. Quick test

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/jobs -F "file=@scan.png"   # {"job_id": "..."}
curl -s http://127.0.0.1:8000/jobs/<job_id>                        # status + result JSON
```

Accepted types: pdf, jpg, jpeg, png, tiff. Anything else returns HTTP 400. Processing is synchronous, so the job is already `done` or `error` when `POST /jobs` returns.

### 4. Live frontend against the backend

Open `editor.html?job=<job_id>`. If the UI and API run on different origins, add `&api=http://127.0.0.1:8000`. Cross-origin calls need CORS on the backend until the same-origin serving branch is merged.

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
| `app/main.py` | FastAPI endpoints: `GET /health`, `POST /jobs`, `GET /jobs/{job_id}` |
| `app/db.py` | SQLite jobs table (`init_db`, `create_job`, `get_job`, `set_result`) |
| `app/storage.py` | Byte-for-byte masters: SHA-256 hashed before write, verified after |
| `app/router.py` | Per-page quality metrics (blur, contrast, noise, skew_deg) → **FAST** / **HEAVY** badge |
| `app/pdfutil.py` | PDF via pypdfium2, images via cv2; long side capped at 1600 px |
| `app/ocr.py` | Lazy PaddleOCR fast pass (PP-OCRv5_mobile_det + ta_PP-OCRv5_mobile_rec); stub result if paddle is absent |
| `app/schema_out.py` | Builds the frozen output contract JSON (`schema/schema.json`) |

The output contract is in [`schema/schema.json`](schema/schema.json), the endpoint reference in [`schema/endpoints.md`](schema/endpoints.md), and a sample page in [`schema/doc_demo.json`](schema/doc_demo.json).

## Frontend layout

| File | Role |
| --- | --- |
| `index.html`, `upload.js`, `upload.css` | Upload screen and stepper |
| `editor.html`, `editor.js`, `editor.css` | Review editor |
| `api.js` | Data access: live backend or `?mock=1` demo |
| `translit.js` | Offline Tanglish → Tamil transliteration for fixes |
| `tokens.css`, `ui.css` | Design tokens and shared components |

## Design system

| Folder | Contents |
| --- | --- |
| `design system/foundations/` | Color systems, typography specimen, design brief, and shared styles |
| `design system/components/` | Component sheet, library, and standalone button/toggle examples |
| `design system/layout/` | Three-page layout plan and annotated editor mock |

Open the HTML design files in a browser. No build step is needed.

## Where it goes next

- Send HEAVY pages through image repair and a heavier OCR pass.
- Save reviewer corrections to the server so fixes persist and can improve later passes.
- Wire the editor to the TXT export and processing receipt that ship with the export branch.
- Keep runtime assets local. Any hosted OCR route remains a project decision.

## Heavy repair and OCR provider evaluation

The experimental CPU repair pipeline and OCR comparisons are in `repair/`, `text_filter.py`, and `ocr_outputs/`. This evaluation does **not** change the webapp backend: the app still uses its configured PaddleOCR path; Sarvam has been tested as a hosted alternative, not wired into the app.

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
