#!/usr/bin/env bash
# Pink Cloud editor slice smoke test. Static checks only, no server needed.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
ok()   { echo "ok   $1"; }
bad()  { echo "FAIL $1"; fail=1; }

# 1. All slice files exist.
for f in schema/schema.json schema/doc_demo.json tokens.css ui.css editor.css \
         index.html editor.html api.js translit.js editor.js; do
  if [ -f "$f" ]; then ok "exists $f"; else bad "missing $f"; fi
done

# 2. JSON parses and the mock honours the data contract.
PY=(python3)
if ! "${PY[@]}" --version >/dev/null 2>&1; then PY=(py -3); fi
"${PY[@]}" - <<'EOF'
import json, re, sys

schema = json.load(open("schema/schema.json", encoding="utf-8"))
doc = json.load(open("schema/doc_demo.json", encoding="utf-8"))

root_allowed = set(schema["properties"].keys())
assert schema.get("additionalProperties") is False, "root must be closed"
for req in ["page", "profile", "quality", "lines", "text"]:
    assert req in schema.get("required", []), "missing required " + req
    assert req in doc, "mock missing " + req
assert set(doc.keys()) <= root_allowed, "mock has fields outside the contract"
assert doc["profile"] in ("FAST", "HEAVY")
for req in ["blur", "contrast", "noise", "skew_deg"]:
    assert req in doc["quality"], "quality missing " + req
seqs = [l["seq"] for l in doc["lines"]]
assert seqs == sorted(seqs), "lines not in seq order"
for l in doc["lines"]:
    assert re.match(r"^L[0-9]+$", l["id"]), "bad line id " + l["id"]
    assert len(l["bbox"]) == 4, "bbox must be [x,y,w,h]"
    assert 0 <= l["confidence"] <= 1
stitched = "\n".join(l["body"] for l in sorted(doc["lines"], key=lambda l: l["seq"]))
assert doc["text"] == stitched, "text must stitch line bodies in seq order"

assert doc["page"] == 1 and doc["preprocessed"] is True and doc["needs_review"] is True
l3 = [l for l in doc["lines"] if l["id"] == "L3"][0]
c = doc["corrections"][0]
assert c["before"] in l3["body"], "correction target must be the OCR text"
assert c["after"] != c["before"] and c["tier"] in ("T1", "T2", "T3") and c["evidence"]
print("ok   contract: mock validates, text stitches, correction target present")
EOF
[ $? -eq 0 ] || fail=1

# 3. Tanglish transliteration spot-checks (exercises the real table).
node -e '
const fs = require("fs");
const window = {};
eval(fs.readFileSync("translit.js", "utf8"));
const t = window.PC_Translit.transliterate;
const cases = [
  ["vaalaRivan", "வாலறிவன்"],
  ["maram", "மரம்"],
  ["thamizh", "தமிழ்"],
  ["aa", "ஆ"],
  ["kattrathanaal", "கட்ட்ரதநால்"]
];
let bad = 0;
for (const [input, want] of cases) {
  const got = t(input);
  if (got === want) { console.log("ok   translit " + input + " -> " + got); }
  else { console.log("FAIL translit " + input + " -> " + got + " (want " + want + ")"); bad = 1; }
}
process.exit(bad);
'
[ $? -eq 0 ] || fail=1

# 4. No hard-coded hex outside tokens.css.
if grep -rEn '#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?\b' ui.css editor.css index.html editor.html api.js translit.js editor.js; then
  bad "hard-coded hex outside tokens.css"
else
  ok "no hard-coded hex outside tokens.css"
fi

# 5. Never purple, anywhere in the slice.
if grep -rin 'purple' tokens.css ui.css editor.css index.html editor.html api.js translit.js editor.js schema/; then
  bad "found purple"
else
  ok "no purple anywhere"
fi

# 6. No network calls: no http, no CDN hints in page code.
if grep -rEn 'http|cdn' index.html editor.html api.js translit.js editor.js ui.css editor.css; then
  bad "network reference in page code"
else
  ok "offline: no network references"
fi

# 7. Fonts directory state (warn only: binaries are dropped in by hand).
if ls fonts/*.woff2 fonts/*.ttf >/dev/null 2>&1; then
  ok "font files present"
else
  echo "warn ./fonts/ is empty (satoshi-regular/medium/bold .woff2 + noto-sans-tamil.ttf pending)"
fi

if [ "$fail" -eq 0 ]; then echo "SMOKE PASS"; else echo "SMOKE FAIL"; fi
exit "$fail"
