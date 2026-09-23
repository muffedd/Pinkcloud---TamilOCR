/* eKalappai-style Tanglish -> Tamil mapping table + tokenizer.
   Pure offline lookup. Consonant + vowel-sign composition, so
   "vaalarivan" becomes the Tamil word with the correct final letter. */

(function () {
  "use strict";

  var PULLI = "்";

  /* Consonant base letters (uyir-mei stem without pulli). */
  var BASE = {
    k: "க", g: "க",
    ng: "ங",
    ch: "ச", c: "ச", s: "ச",
    S: "ஸ", sh: "ஷ",
    j: "ஜ",
    t: "ட", d: "ட",
    N: "ண",
    th: "த", dh: "த",
    n: "ந",
    p: "ப", b: "ப",
    m: "ம",
    y: "ய",
    r: "ர", R: "ற",
    l: "ல", L: "ள",
    v: "வ", w: "வ",
    zh: "ழ",
    h: "ஹ",
    ksh: "க்ஷ"
  };

  /* Vowel marks: roman -> [standalone letter, combining sign].
     Sign "" means the inherent vowel (bare consonant letter). */
  var VOWELS = {
    aa: ["ஆ", "ா"], A: ["ஆ", "ா"],
    ai: ["ஐ", "ை"],
    au: ["ஔ", "ௌ"],
    ee: ["ஈ", "ீ"], I: ["ஈ", "ீ"],
    oo: ["ஊ", "ூ"], U: ["ஊ", "ூ"],
    ae: ["ஏ", "ே"], E: ["ஏ", "ே"],
    oe: ["ஓ", "ோ"], O: ["ஓ", "ோ"],
    a: ["அ", ""],
    i: ["இ", "ி"],
    u: ["உ", "ு"],
    e: ["எ", "ெ"],
    o: ["ஒ", "ொ"]
  };

  function sortedKeys(obj) {
    return Object.keys(obj).sort(function (a, b) { return b.length - a.length; });
  }

  var CKEYS = sortedKeys(BASE);
  var VKEYS = sortedKeys(VOWELS);

  function matchKey(src, i, keys) {
    for (var k = 0; k < keys.length; k++) {
      if (src.startsWith(keys[k], i)) return keys[k];
    }
    return null;
  }

  function isWordChar(ch) {
    return ch !== undefined && ch !== null && /[A-Za-z]/.test(ch);
  }

  function transliterate(src) {
    var out = "";
    var i = 0;
    while (i < src.length) {
      var ch = src[i];
      /* Aytham stands alone. */
      if (ch === "q") {
        out += "ஃ";
        i += 1;
        continue;
      }
      var c = matchKey(src, i, CKEYS);
      if (c) {
        var v = matchKey(src, i + c.length, VKEYS);
        if (v) {
          out += BASE[c] + VOWELS[v][1];
          i += c.length + v.length;
        } else {
          var next = src[i + c.length];
          /* Word-final n is the Tamil letter, not the dental one. */
          if (c === "n" && !isWordChar(next)) {
            out += "ன்";
          } else {
            out += BASE[c] + PULLI;
          }
          i += c.length;
        }
        continue;
      }
      var vow = matchKey(src, i, VKEYS);
      if (vow) {
        out += VOWELS[vow][0];
        i += vow.length;
        continue;
      }
      out += ch;
      i += 1;
    }
    return out;
  }

  window.PC_Translit = { transliterate: transliterate };
})();
