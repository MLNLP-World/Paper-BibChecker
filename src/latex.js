// LaTeX 重音解码与文本归一化。
// 对应 Python：bibchecker/matching.py 和 checker.py 中的 _decode_latex / _normalize_text。
//
// 注意：checker.py 与 matching.py 各有一份 _normalize_text，二者略有差别：
//   - matching 版：_decode_latex → "&"→" and " → NFKD → 去组合记号 → [^a-z0-9]+→空格
//   - checker 版：_decode_latex → 去 \cmd → 去 {} → NFKD → 去组合记号 → [^a-z0-9]+→空格
// checker 版驱动最终分类（_title_similarity），是规范实现。两份都提供以便逐例对齐。

const ACCENTS = {
  "'": "́",
  "`": "̀",
  "^": "̂",
  '"': "̈",
  "~": "̃",
  "=": "̄",
  ".": "̇",
  u: "̆",
  v: "̌",
  H: "̋",
  c: "̧",
  k: "̨",
  b: "̱",
  d: "̣",
  r: "̊",
};

// 对应 Python 正则：
//   \{?\\(?P<accent>['`^"~=\.uvHckbdr])(?:\{(?P<braced>[A-Za-z])\}|(?P<plain>[A-Za-z]))\}?
// JS 无命名组混用问题，这里用编号组。accent 字符类里的字符需转义。
const ACCENT_RE =
  /\{?\\(['`^"~=.uvHckbdr])(?:\{([A-Za-z])\}|([A-Za-z]))\}?/g;

const SPECIAL = [
  ["\\ss", "ß"],
  ["\\o", "ø"],
  ["\\O", "Ø"],
  ["\\l", "ł"],
  ["\\L", "Ł"],
  ["\\ae", "æ"],
  ["\\AE", "Æ"],
  ["\\oe", "œ"],
  ["\\OE", "Œ"],
];

export function decodeLatex(value) {
  if (!value) return "";
  let out = String(value).replace(ACCENT_RE, (match, accent, braced, plain) => {
    const letter = braced || plain || "";
    return (letter + ACCENTS[accent]).normalize("NFC");
  });
  for (const [command, replacement] of SPECIAL) {
    out = out.split(command).join(replacement);
  }
  return out;
}

// 去除 Unicode 组合记号（对应 Python 的 unicodedata.combining 过滤）。
function stripCombining(value) {
  return value.replace(/\p{M}/gu, "");
}

// checker.py 版 _normalize_text —— 规范实现，用于标题相似度与分类。
export function normalizeText(value) {
  if (!value) return "";
  let out = decodeLatex(String(value));
  out = out.replace(/\\[A-Za-z]+\s*/g, "").replace(/\{/g, "").replace(/\}/g, "");
  out = stripCombining(out.normalize("NFKD").toLowerCase());
  out = out.replace(/[^a-z0-9]+/g, " ");
  return out.split(/\s+/).filter(Boolean).join(" ");
}

// matching.py 版 _normalize_text —— 用于该模块内部逐例对齐（"&"→" and "）。
export function normalizeTextMatching(value) {
  if (!value) return "";
  let out = decodeLatex(String(value)).replace(/&/g, " and ");
  out = stripCombining(out.normalize("NFKD").toLowerCase());
  out = out.replace(/[^a-z0-9]+/g, " ");
  return out.split(/\s+/).filter(Boolean).join(" ");
}
