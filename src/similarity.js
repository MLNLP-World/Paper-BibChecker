// 标题/文本相似度。核心是忠实复刻 Python difflib.SequenceMatcher.ratio()，
// 因为标题相似度直接驱动分类，任何偏差都会让 JS 版与 Python 版结论不一致。
//
// 对应 Python：
//   - difflib.SequenceMatcher(None, a, b).ratio()
//   - bibchecker/checker.py 与 matching.py 的 _title_similarity / _year_similarity
import { normalizeText } from "./latex.js";

// 忠实移植 difflib.SequenceMatcher（isjunk=None, autojunk=True）。
// 序列元素为字符（Python 对字符串按字符序列处理）。
class SequenceMatcher {
  constructor(a, b) {
    this.a = a;
    this.b = b;
    this._chainB();
  }

  _chainB() {
    const b = this.b;
    const b2j = new Map();
    for (let i = 0; i < b.length; i++) {
      const elt = b[i];
      let idxs = b2j.get(elt);
      if (!idxs) {
        idxs = [];
        b2j.set(elt, idxs);
      }
      idxs.push(i);
    }
    // isjunk=None，无 junk。仅处理 autojunk 的 popular 清除。
    const n = b.length;
    const popular = new Set();
    if (n >= 200) {
      const ntest = Math.floor(n / 100) + 1;
      for (const [elt, idxs] of b2j) {
        if (idxs.length > ntest) popular.add(elt);
      }
      for (const elt of popular) b2j.delete(elt);
    }
    this.b2j = b2j;
    this.bjunk = new Set(); // 空：isjunk=None
  }

  findLongestMatch(alo, ahi, blo, bhi) {
    const { a, b, b2j } = this;
    let besti = alo;
    let bestj = blo;
    let bestsize = 0;
    let j2len = new Map();
    for (let i = alo; i < ahi; i++) {
      const newj2len = new Map();
      const indices = b2j.get(a[i]) || [];
      for (const j of indices) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) || 0) + 1;
        newj2len.set(j, k);
        if (k > bestsize) {
          besti = i - k + 1;
          bestj = j - k + 1;
          bestsize = k;
        }
      }
      j2len = newj2len;
    }
    // isjunk=None，故只做“非 junk 扩展”，两处 junk 扩展循环恒不触发。
    while (
      besti > alo &&
      bestj > blo &&
      a[besti - 1] === b[bestj - 1]
    ) {
      besti--;
      bestj--;
      bestsize++;
    }
    while (
      besti + bestsize < ahi &&
      bestj + bestsize < bhi &&
      a[besti + bestsize] === b[bestj + bestsize]
    ) {
      bestsize++;
    }
    return [besti, bestj, bestsize];
  }

  matchingBlocksSize() {
    const la = this.a.length;
    const lb = this.b.length;
    const queue = [[0, la, 0, lb]];
    let total = 0;
    while (queue.length) {
      const [alo, ahi, blo, bhi] = queue.pop();
      const [i, j, k] = this.findLongestMatch(alo, ahi, blo, bhi);
      if (k) {
        total += k;
        if (alo < i && blo < j) queue.push([alo, i, blo, j]);
        if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
      }
    }
    return total;
  }

  ratio() {
    const length = this.a.length + this.b.length;
    if (!length) return 1.0;
    return (2.0 * this.matchingBlocksSize()) / length;
  }
}

export function sequenceRatio(a, b) {
  return new SequenceMatcher(a, b).ratio();
}

// 对应 checker.py / matching.py 的 _title_similarity（两份实现相同）。
export function titleSimilarity(left, right) {
  const l = normalizeText(left);
  const r = normalizeText(right);
  if (!l || !r) return 0.0;
  if (l === r) return 1.0;
  const leftTokens = new Set(l.split(" "));
  const rightTokens = new Set(r.split(" "));
  let overlap = 0;
  for (const t of leftTokens) if (rightTokens.has(t)) overlap++;
  const union = new Set([...leftTokens, ...rightTokens]).size;
  const jaccard = union ? overlap / union : 0.0;
  const containment = overlap / Math.min(leftTokens.size, rightTokens.size);
  return Math.max(sequenceRatio(l, r), 0.55 * jaccard + 0.45 * containment);
}

// 对应 _year_similarity：相同=1.0，差一年=0.7，其余=0.0，任一为空=null。
export function yearSimilarity(expected, actual) {
  if (expected == null || actual == null) return null;
  const diff = Math.abs(expected - actual);
  return diff === 0 ? 1.0 : diff === 1 ? 0.7 : 0.0;
}

export function yearsCompatible(left, right) {
  return left == null || right == null || Math.abs(left - right) <= 1;
}
