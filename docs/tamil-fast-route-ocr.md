# Lightweight open-source Tamil OCR for the FAST route

> **Note (repo cleanup):** the raw scans now live at `samples/editor/sample1.png` and `sample2.png`. The repair outputs (`repair/out/`, `repair/verify/`), ground-truth text (`gt_*.txt`) and most `ocr_outputs/` artifacts referenced below were removed from the tree and are git-ignored; they are in git history (e.g. commit `3c53e7f`). Only `ocr_outputs/sarvam/raw/sample*/json/` and `.../metadata/page_001.json` remain, as test fixtures.

**Question:** the app currently sends both FAST and HEAVY pages to Sarvam's hosted
Document AI. Find a lightweight, open-source, Tamil-supporting, low-latency model to
replace the **FAST** route (clean pages), leaving Sarvam for HEAVY (damaged) pages.
PaddleOCR PP-OCRv5 is excluded by request.

Research date: 2026-09-23. Every claim below is from a primary source (model card,
config, license file, paper, or first-party repo); links are at the end.

## Where it plugs in

`fastapi/sqlite/app/ocr.py::ocr_page(img)` is the single seam. It returns
`[{body, bbox, confidence}]` and today calls Sarvam only (PaddleOCR has been removed).
`fastapi/sqlite/app/router.py::choose_profile()` already labels each page FAST or
HEAVY. So a fast-route model is a **second engine branch**: FAST → local open model,
HEAVY → Sarvam. No contract change is needed if the model emits per-line `body`,
`bbox`, and `confidence`.

## Shortlist

| Model | Params / disk | Tamil evidence | License | Latency (as published) | bbox + confidence | Domain fit |
|---|---|---|---|---|---|---|
| **Surya 2** | 650M / 1.3 GB | **89.9%** Tamil in a 91-language benchmark | weights OpenRAIL-M (free < $5M); code Apache-2.0 | 5.35 pages/s, p50 18.9 s @128 concurrent on RTX 5090; ~9.3 s/page Apple Silicon | **both** (per-block bbox + mean token prob) | documents |
| **dots.ocr** | 1.7B / ~5.8 GB | 100-language in-house bench; "low-resource" claim, no Tamil number | **MIT** | 1.7B VLM, GPU | layout bbox + text (confidence not documented) | documents |
| **sair390/tamil-ocr-qwen25vl** | ~3.8B / ~7.5 GB | fine-tuned on 199,925 Tamil samples (~75K real book pages) | **Apache-2.0** | 3B VLM, GPU | text; bbox not documented | Tamil books/scans |
| **ocr_tamil** (CRAFT + PARSeq) | ~50M / 169 MB | "Tamil > 95%", scene text | **MIT** | claims 10–40% faster than EasyOCR/Tesseract | CRAFT bbox + PARSeq prob | natural scenes (out of domain) |
| **Tesseract + anuvaad_tam** | — / 12 MB | Anuvaad Tamil traineddata | **MIT** | fastest, CPU | line/word bbox + conf | documents |
| **Tesseract tessdata_fast `tam`** | — / 3.2 MB | official Tamil model | Apache-2.0 | fastest, CPU | bbox + conf | documents |
| **EasyOCR `ta`** | CRNN / ~small | Tamil supported (`tamil_g1`) | Apache-2.0 | CPU ok, GPU better | bbox + conf | documents/scene |
| **GOT-OCR2.0** | 580M / 1.3 GB | paper: "mainly supports English and Chinese" | Apache-2.0 | VLM | limited | general |
| **SHAON123/indicpage-ocr-tamil** | GOT-OCR2.0 + LoRA | Tamil fine-tune, no eval numbers | Apache-2.0 | VLM | not documented | Tamil |
| **Tamizhi-Net-OCR** | Tesseract 4.1.1 LSTM | Tamil/Sinhala **legacy fonts** | research code | fast | bbox | legacy print |
| **IndicPhotoOCR** | detection + rec | 11 Indian languages incl. Tamil | **MIT** | scene-text toolkit | bbox | scene text |

## Recommendation

**Primary: Surya 2 for the FAST route.** It is the only candidate with a *measured*
Tamil accuracy in the lightweight class (89.9%), it ships a dedicated text-line
detector plus a VLM recogniser, and its output already carries per-block `bbox` and
`confidence` — a direct fit for `ocr_page()`'s contract. Serve it with `vllm` on a
GPU for low latency, or `llama.cpp` on CPU/Apple Silicon if no GPU is available.

**Two caveats to decide before adopting it:**
1. **License.** Weights are a modified AI Pubs Open Rail-M: free for research,
   personal use, and startups under $5M funding/revenue; broader commercial use needs
   a paid licence from Datalab. The code is Apache-2.0.
2. **Latency needs a GPU.** On Apple Silicon it is ~0.1 pages/s (~9–10 s/page). A
   modest NVIDIA GPU is required to beat the current Sarvam cloud call.

**If a permissive licence is mandatory:** `dots.ocr` (**MIT**, 1.7B) is the strongest
open alternative and unifies layout + recognition with bboxes, at a higher compute
cost. For a pure CPU fast path, `Tesseract + anuvaad_tam` (**MIT**, 12 MB) is the
lightest document-domain option.

**Do not adopt blind:** `GOT-OCR2.0` only "mainly supports English and Chinese"; its
Tamil options are community LoRAs with no published evaluation. The scene-text models
(`ocr_tamil`, `IndicPhotoOCR`) are out of domain for aged printed pages.

**Sarvam has no open OCR weights** — only LLMs (`sarvam-1`, `sarvam-30b`,
`sarvam-105b`, `sarvam-m`) are published, so Sarvam stays the hosted HEAVY route.

## Benchmark plan (before wiring anything)

Score candidates on the existing samples with the tooling already in the repo, then
compare against the recorded Sarvam/Paddle numbers in `OCR_API_COMPARISON.md`:

```sh
# clean page, has ground truth
python3 scripts/cer.py gt_cict_narrinai_p3.txt <candidate>_sample1.txt
# damaged page, no matching GT: compare Tamil ratio / garbage / repeat-loop lines
python3 scripts/text_filter.py <candidate>_sample1.txt --json <candidate>_sample1.json
```

Required per candidate: `samples/editor/sample1.png` (clean, GT available) and `samples/editor/sample2.png`
(damaged). Record Tamil ratio, garbage rate, repeat-loop lines, mean confidence, CER
(sample1 only), and wall-clock seconds per page on the target hardware. Only promote a
candidate to the FAST route if it beats the current Sarvam latency **and** does not
regress sample1 CER.

## Hardware fit — Lenovo LOQ, RTX 4050 6GB, i5-12450HX

6GB VRAM is enough for a FAST-route model, but **not with fp16 or vLLM**: vLLM
over-reserves VRAM (CUDA graphs + KV cache) and is a poor fit at 6GB. Use a
quantised **llama.cpp** build instead. Verified weight sizes:

| Model (runtime) | Weights on disk | 6GB verdict |
|---|---:|---|
| **Surya 2** (`surya-2.gguf` + `surya-2-mmproj.gguf`) | 1207 + 195 = **1.4 GB** | Fits comfortably |
| **dots.ocr** (`dots.ocr-Q8_0.gguf` + mmproj Q8_0) | 1806 + 1281 = **3.1 GB** | Fits, but cap context |
| GOT-OCR2.0 (fp16) | 1.3 GB | Fits |
| ocr_tamil (CRAFT + PARSeq) | 0.17 GB | Trivial, fastest |
| Tesseract + anuvaad_tam | 0.012 GB | CPU only |
| sair390/tamil-ocr-qwen25vl (3.8B) | ~3 GB at 4-bit (no GGUF published) | Possible, tight |

Surya's `llamacpp` backend already defaults to `LLAMA_CPP_NGL=99` (all layers on
the GPU) and offloads the vision projector, so it will use the 4050 without extra
flags. **The default KV budget is the trap:** `ctx_size = max(16384, parallel ×
12288)` = **98,304 tokens** at the default 8 slots, which will not fit in 6GB.
Cap it:

```sh
export SURYA_INFERENCE_BACKEND=llamacpp
# SURYA_INFERENCE_NGL is LLAMA_CPP_NGL (default 99 = all layers on GPU)
export SURYA_INFERENCE_PARALLEL=1
export SURYA_INFERENCE_CTX_SIZE=16384     # or SURYA_INFERENCE_CTX_PER_SLOT=8192
```

Then ~1.4GB weights + a few hundred MB KV leaves headroom on the 4050. Expected
latency is a few seconds/page and **must be measured** — the published 5.35 pages/s
figure is an RTX 5090 under load, not a 6GB laptop GPU. The i5-12450HX is a fine CPU
fallback for Tesseract, and can host `llama-server` with partial GPU offload if VRAM
runs short. You can also point Surya at an external server with
`SURYA_INFERENCE_URL`.

## Sources

- Surya 2 — https://huggingface.co/datalab-to/surya-ocr-2 (params, Tamil 89.9%, confidence, throughput, licence); 91-language table: https://github.com/datalab-to/surya/blob/master/static/docs/multilingual.md
- dots.ocr — https://huggingface.co/dots-studio/dots.ocr (MIT, 1.7B, layout+text, 100-language bench)
- sair390/tamil-ocr-qwen25vl — https://huggingface.co/sair390/tamil-ocr-qwen25vl (Apache-2.0, training data)
- ocr_tamil (CRAFT + PARSeq) — https://huggingface.co/GnanaPrasath/ocr_tamil and https://github.com/gnana70/tamil_ocr (MIT, sizes, scene-text claim)
- Tesseract Tamil — https://github.com/tesseract-ocr/tessdata_fast (`tam.traineddata`, 3.2 MB); Anuvaad models: https://github.com/project-anuvaad/anuvaad-ocr-model (MIT, `anuvaad_tam.traineddata`, 12 MB)
- EasyOCR Tamil — https://github.com/JaidedAI/EasyOCR (`tamil_g1`)
- GOT-OCR2.0 — https://huggingface.co/stepfun-ai/GOT-OCR2_0 and https://arxiv.org/abs/2409.01704 ("mainly supports English and Chinese"); Tamil LoRA: https://huggingface.co/SHAON123/indicpage-ocr-tamil
- Tamizhi-Net-OCR — https://github.com/aaivu/Tamizhi-Net-OCR (Tesseract LSTM for Tamil/Sinhala legacy fonts)
- IndicPhotoOCR — https://github.com/Bhashini-IITJ/IndicPhotoOCR (MIT, 11 Indic languages, scene text)
- Sarvam published models — https://huggingface.co/sarvamai (LLMs only; no OCR weights)
