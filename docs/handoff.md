Works: editor loads doc_demo.json offline, draws every line/word box on the scan, syncs clicks both ways, J/K walk the doubt queue, and the fix popup turns வாழறிவன் into வாலறிவன் on Accept (marks human + toast).
Test: serve this folder over http (python3 -m http.server), open index.html, press J, then Enter, then Accept.
Touched: schema/schema.json, schema/doc_demo.json, tokens.css, ui.css, editor.css, index.html, api.js, translit.js, editor.js, scripts/smoke.sh.
Gaps: ./fonts/ is empty so Satoshi/Noto fall back; Export only toasts (page 3 is another slice); single mock page so J/K stay on page 1.
