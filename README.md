# Pinkcloud---TamilOCR

Web tool for reading Tamil and English documents from scans.

## Status

Early. Design foundations are done. Application code is not written yet.

## Design system

| File | What it holds |
| --- | --- |
| `colourscheme.html` | Colour foundations. Tokens, swatches, scan states, principles. |
| `styles.css` | Tokens and layout for `colourscheme.html`. |
| `comps.html` | Component sheet. Button states, icon buttons, modal footer. |

Open the HTML files in a browser. No build step. No server needed.

## Colour rules

- Warm neutrals carry most of the view. Target: 80–90% neutral.
- Orange `#F25A1A` is the brand accent. It marks main actions and live scan only.
- Orange is not a warning colour. Warning amber `#B67816` is separate.
- Green = pass. Amber = check. Red = fault. Blue = note.
- No pure black. No pure white. No purple.
- Colour never stands alone. Each state also carries an icon and a word.

## Files in the pipeline

- `design-system-spec.txt` — source brief for the colour page.
- `pink-cloud-layout-plan-rename-to-.html` — layout plan draft.

## Notes

- Plain HTML and CSS only. No frameworks, no scripts, no external fonts.
- Font stack: Satoshi (local), then system fallbacks.
- Built for 360px width and up.
