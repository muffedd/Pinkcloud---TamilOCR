# Pink Cloud — Tamil OCR

Pink Cloud is an early-stage tool for reading scanned Tamil and English books. Its planned workflow repairs low-quality pages, supports human corrections, and exports searchable text.

## Status

Design foundations are in progress. The application code is not written yet.

## Design system

| Folder | Contents |
| --- | --- |
| `design system/foundations/` | Color systems, typography specimen, design brief, and shared styles. |
| `design system/components/` | Component sheet, library, and standalone button/toggle examples. |
| `design system/layout/` | Three-page layout plan. |

Open the HTML design files in a browser. No build step is needed.

## Product direction

- Upload multi-page PDF and image scans, including TIFF.
- Run a fast OCR pass on every page and send doubtful pages through repair and heavier OCR.
- Review the scan beside Unicode Tamil text; make corrections explicit and traceable.
- Export searchable PDF, TXT, and a processing receipt.
- Keep runtime assets local. Any hosted OCR route remains a project decision.

## Project references

- `design system/foundations/design-system-spec.txt` — design system brief.
- `schema/schema.json` and `schema/doc_demo.json` — OCR output contract and demo page.

