# Hosted API OCR for the FAST route (free / large free tier)

**Question:** replace the local-model plan with **hosted APIs**. Same criteria as
before — Tamil, low latency, good fit for the FAST route — plus a hard constraint:
the API key must be **free or have a large free tier**. Sarvam stays the HEAVY route.
PaddleOCR is excluded per the earlier request.

Research date: 2026-09-23. Free-tier quotas change often; verify on the linked pricing
page before committing. Every claim below is from a primary source (provider docs,
language tables, or API references); links are at the end.

## Where it plugs in

`fastapi/sqlite/app/ocr.py::ocr_page(img)` returns `[{body, bbox, confidence}]` and
today calls Sarvam only (PaddleOCR has been removed). A hosted FAST engine would be a
second branch next to `sarvam`. **Contract fit matters:** an OCR API that returns line/word boxes and
confidence drops in with no downstream change; a chat-style VLM that returns only text
does not.

## Shortlist

| API | Tamil | Free tier | bbox + confidence | Contract fit |
|---|---|---|---|---|
| **Azure AI Vision Read (F0)** | ✅ printed (`ta`) | F0: 20 calls/min; 4 MB free-tier file limit; 1 page = 1 transaction (monthly F0 allowance commonly 5,000 — confirm on pricing) | line bbox + **per-word confidence** | **native** |
| **Mistral OCR 4.1** | ✅ (South Asian list) | Free plan (limited); then per-1,000-pages | paragraph bbox + structural labels + **block confidence** | **native** |
| **Google Cloud Vision** | ✅ | first 1,000 units/month free (confirm on pricing) | word bbox + confidence | **native** |
| **Google Gemini API (AI Studio)** | ✅ | free tier with very large token limits | text only (no reliable bbox/conf) | needs a detector |
| **OCR.space** | ✅ (Tesseract engine) | **25,000 requests/month**, 1 MB, 3 PDF pages | text + word bbox, **no confidence** | partial |
| **Groq** (Llama 4 Scout/Maverick) | multilingual VLM | free plan | text only | needs a detector |
| ~~Hugging Face Inference Providers~~ | — | $0.10/month free credit | — | too small |

## Latency-first ranking

The FAST route handles clean pages, so speed matters more than the last point of
accuracy. Provider-published decode speed and free tiers:

| API | Vision model | Published speed | Free tier | Output |
|---|---|---|---|---|
| **Cerebras** | `qwen-3.8-27b` (multimodal) | **~1850 tok/s** | Free trial: **5 RPM, 1M tokens/day**; image input (2/request, 10 MiB) | text |
| **Groq** | `qwen/qwen3.8-27b` (multimodal) | **450 tok/s** | Free plan: **30 RPM, 1K RPD, 200K tokens/day** | text |
| **Google Cloud Vision** | CNN OCR | non-autoregressive (~1 s/page) | 1,000 units/month | text + word bbox + confidence |
| **Azure Read** | CNN OCR | non-autoregressive | F0: 20 calls/min, 1 page = 1 transaction | text + line bbox + word confidence |
| **OCR.space** | Tesseract | fast, non-autoregressive | **25,000 requests/month** | text (+word bbox), no confidence |
| **Mistral OCR 4.1** | document OCR | moderate | free plan (limited) | paragraph bbox + block confidence |

A full page is roughly 2,000 output tokens, so decode speed dominates: Cerebras
finishes in ~1 s, Groq in ~4 s, and the CNN OCR APIs (Google Vision, Azure, OCR.space)
are also ~1 s because they are not autoregressive.

## Recommendation (latency-first)

1. **Cerebras `qwen-3.8-27b` — fastest usable free option.** ~1850 tok/s decode with a
   free trial of 5 RPM and **1M tokens/day** (~500 pages/day), image input supported.
   Trade-off: text-only, so the FAST route loses per-line bbox and confidence.
2. **Groq `qwen/qwen3.8-27b` — same model, more requests, slower pages.** 450 tok/s
   but 30 RPM and only 200K tokens/day (~100 pages/day). Pick it when concurrent
   requests matter more than per-page latency.
3. **If bbox + confidence are required: Google Cloud Vision** (word boxes + confidence,
   ~1 s, but only 1,000 units/month free) or **Azure Read** (line boxes + word
   confidence, larger F0 allowance). These are the fastest options that keep the
   existing `{body, bbox, confidence}` contract.
4. **OCR.space** — 25,000 requests/month free and fast, but Tesseract is weak on aged
   Tamil print and returns no confidence.
5. **Rule out as primary:** Gemini/Groq/Cerebras VLMs if you need confidence;
   SambaNova (signup credits only); HF Inference Providers ($0.10/month).

**The key trade-off:** the lowest-latency free tiers (Cerebras, Groq) are text-only
VLMs. The editor's doubt queue is built on per-line confidence, so a text-only FAST
engine must fall back to the router's CV metrics as a proxy — or you keep Google
Vision/Azure for the FAST route and accept a smaller free quota.

## Notes that affect the choice

- **Confidence is the differentiator.** Azure, Mistral, and Google Vision return a
  confidence value; Gemini, Groq, and OCR.space do not. The editor's doubt queue is
  built on confidence, so a text-only API would need the router's CV metrics as a
  proxy.
- **File-size limits.** Azure's free tier caps files at 4 MB; OCR.space caps at 1 MB.
  Downscale/compress before upload, or the free tier will reject pages.
- **Page-count caps.** OCR.space free allows only 3 PDF pages per request; Azure bills
  per page and limits free files to 2 pages for PDF/TIFF. Send single-page images.
- **Billing still needs a card** for Azure and Google Cloud even on the free tier;
  Mistral's free plan and OCR.space's free key do not.

## Benchmark plan

Run each candidate on `raw/sample1.png` (clean, ground truth available) and
`raw/sample2.png` (damaged), then score with the repo tooling and compare to the
recorded Sarvam/Paddle numbers in `OCR_API_COMPARISON.md`:

```sh
python3 scripts/cer.py gt_cict_narrinai_p3.txt <candidate>_sample1.txt
python3 scripts/text_filter.py <candidate>_sample1.txt --json <candidate>_sample1.json
```

Record Tamil ratio, garbage rate, repeat-loop lines, mean confidence, CER (sample1
only), and wall-clock seconds per page. Promote a candidate to the FAST route only if
it beats the current Sarvam latency without regressing sample1 CER.

## Sources

- Azure Read — language support (Tamil `ta` ✅): https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/language-support; response has line bboxes + per-word confidence and free-tier limits: https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/how-to/call-read-api; pricing: https://azure.microsoft.com/pricing/details/cognitive-services/computer-vision/
- Cerebras — model catalog (qwen-3.8-27b multimodal, ~1850 tok/s): https://inference-docs.cerebras.ai/models/overview; free-trial limits (5 RPM, 1M tokens/day, image limits): https://inference-docs.cerebras.ai/support/rate-limits
- Groq — models (qwen/qwen3.8-27b multimodal, 450 tok/s, 20 MB image): https://console.groq.com/docs/models; free-plan limits: https://console.groq.com/docs/rate-limits
- Mistral OCR 4.1 — paragraph bboxes, block labels, block confidence, free plan: https://docs.mistral.ai/capabilities/document_ai/basic_ocr/; Tamil in supported languages: https://docs.mistral.ai/resources/languages
- Google Cloud Vision — free tier and pricing: https://cloud.google.com/vision/pricing
- Google Gemini API — free tier and rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- OCR.space — 25,000 requests/month free, limits: https://ocr.space/ocrapi
- Groq — free plan and vision models: https://console.groq.com/docs/rate-limits
- Hugging Face Inference Providers — $0.10/month free credit: https://huggingface.co/docs/inference-providers/pricing
