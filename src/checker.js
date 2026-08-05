// 候选评分、字段比对与四类分类。移植 bibchecker/checker.py 的核心判定逻辑。
import { normalizeText } from "./latex.js";
import { titleSimilarity, yearSimilarity } from "./similarity.js";
import { compareAuthors, authorSimilarity, matchAuthors, displayAuthor } from "./authors.js";

export const VALIDATED = "validated";
export const NEEDS_REVIEW = "needs_review";
export const LIKELY_HALLUCINATION = "likely_hallucination";
export const UNCONFIRMED = "unconfirmed";

const FIELD_LABELS = {
  title: "标题",
  authors: "作者",
  year: "年份",
  venue: "会议或期刊",
  doi: "DOI",
  arxiv_id: "arXiv ID",
};

const DOI_RE = /10\.\d{4,9}\/[-._;()/:A-Z0-9]+/i;
const ARXIV_RE = /(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?/;

// ---- 条目字段访问 ----
function entryTitle(e) {
  return String(e.getField("title") || "");
}
function entryAuthors(e) {
  const a = e.getField("authors", "author") || [];
  if (typeof a === "string")
    return a.split(/\s+and\s+/i).map((s) => s.trim()).filter(Boolean);
  return a.map((x) => String(x));
}
function entryYear(e) {
  const m = /\d{4}/.exec(String(e.getField("year") || ""));
  return m ? parseInt(m[0], 10) : null;
}
function entryDoi(e) {
  return String(e.getField("doi") || "");
}
function entryArxiv(e) {
  return String(e.getField("arxiv_id", "arxiv", "eprint") || "");
}
function entryVenue(e) {
  return String(e.getField("journal", "booktitle", "venue") || "");
}
function entryKey(e) {
  return String(e.key || "");
}

// ---- 标识符归一化与提取 ----
function normalizeDoi(value) {
  return value
    .trim()
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "")
    .toLowerCase()
    .replace(/[.,;)]+$/, "");
}
function normalizeArxiv(value) {
  value = value.trim().replace(/^.*?arxiv(?:\.org\/(?:abs|pdf)\/|:)/i, "");
  value = value.replace(/\.pdf$/i, "");
  return value.replace(/v\d+$/i, "").toLowerCase();
}
function doiFromCandidate(item) {
  const values = [item.identifier || "", item.url || "", String((item.raw || {}).doi || ""), String((item.raw || {}).DOI || "")];
  for (const v of values) {
    const m = DOI_RE.exec(v);
    if (m) return m[0].replace(/[.,;)]+$/, "");
  }
  return "";
}
function arxivFromCandidate(item) {
  const values = [item.identifier || "", item.url || "", String((item.raw || {}).arxiv_id || ""), String((item.raw || {}).eprint || "")];
  for (const v of values) {
    const m = ARXIV_RE.exec(v);
    if (m) return m[1];
  }
  return "";
}
function identifierEqual(expected, actual, kind) {
  if (!expected || !actual) return false;
  return kind === "doi"
    ? normalizeDoi(expected) === normalizeDoi(actual)
    : normalizeArxiv(expected) === normalizeArxiv(actual);
}

// ---- venue 归一化 ----
function canonicalVenue(value) {
  const aliases = [
    ["tacl", ["tacl", "transactions of the association for computational linguistics"]],
    ["jmlr", ["jmlr", "journal of machine learning research"]],
    ["neurips", ["neurips", "nips", "neural information processing systems"]],
    ["iclr", ["iclr", "international conference on learning representations"]],
    ["icml", ["icml", "international conference on machine learning"]],
    ["cvpr", ["cvpr", "computer vision and pattern recognition"]],
    ["iccv", ["iccv", "international conference on computer vision"]],
    ["eccv", ["eccv", "european conference on computer vision"]],
    ["emnlp", ["emnlp", "empirical methods in natural language processing"]],
    ["acl", ["acl", "annual meeting of the association for computational linguistics"]],
    ["colm", ["colm", "conference on language modeling"]],
    ["arxiv", ["arxiv", "corr"]],
  ];
  for (const [canonical, markers] of aliases)
    for (const marker of markers)
      if (new RegExp(`\\b${marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`).test(value))
        return canonical;
  return "";
}
function venuesEqual(left, right) {
  if (!left || !right) return false;
  const nl = normalizeText(left);
  const nr = normalizeText(right);
  const cl = canonicalVenue(nl);
  const cr = canonicalVenue(nr);
  return cl && cr ? cl === cr : nl === nr;
}

// ---- 首作者匹配（依赖 raw 里预存的文本，见 scoreCandidate）----
function authorsMatchText(left, right) {
  // 复刻 checker.py 的 _authors_match（经 _first_author_match 调用）
  const identity = (author) => {
    const value = normalizeText(author);
    if (!value) return ["", ""];
    if (author.includes(",")) {
      const family = author.split(",")[0];
      const given = author.split(",").slice(1).join(",");
      return [normalizeText(family), normalizeText(given).split(" ").filter(Boolean).map((t) => t[0]).join("")];
    }
    const tokens = value.split(" ");
    return [tokens[tokens.length - 1], normalizeText(tokens.slice(0, -1).join(" ")).split(" ").filter(Boolean).map((t) => t[0]).join("")];
  };
  const [lf, li] = identity(left);
  const [rf, ri] = identity(right);
  if (!lf || !rf) return false;
  const familyMatch = lf === rf || lf.split(" ").pop() === rf.split(" ").pop();
  if (!familyMatch) return false;
  if (!li || !ri) return true;
  return li === ri || li.startsWith(ri) || ri.startsWith(li);
}
function firstAuthorMatch(candidate) {
  const expected = (candidate.raw || {})._expected_first_author_text || "";
  const actual = (candidate.raw || {})._actual_first_author_text || "";
  return authorsMatchText(expected, actual);
}

// ---- 候选评分（移植 _score_candidate）----
export function scoreCandidate(entry, item) {
  const titleScore = titleSimilarity(entryTitle(entry), item.title);
  const authorScore = authorSimilarity(entryAuthors(entry), item.authors);
  const yearScore = yearSimilarity(entryYear(entry), item.year);
  const expectedDoi = entryDoi(entry);
  const expectedArxiv = entryArxiv(entry);
  const candidateDoi = doiFromCandidate(item);
  const candidateArxiv = arxivFromCandidate(item);
  const identifierMatch = Boolean(
    (expectedDoi && candidateDoi && normalizeDoi(expectedDoi) === normalizeDoi(candidateDoi)) ||
      (expectedArxiv && candidateArxiv && normalizeArxiv(expectedArxiv) === normalizeArxiv(candidateArxiv))
  );
  const conflicts = [];
  const evidence = [];
  if (identifierMatch) evidence.push(`${item.source}: 标识符精确匹配`);
  if (titleScore >= 0.88) evidence.push(`${item.source}: 标题相似度 ${titleScore.toFixed(2)}`);
  if (authorScore >= 0.5) evidence.push(`${item.source}: 作者相似度 ${authorScore.toFixed(2)}`);
  if (yearScore === 1.0) evidence.push(`${item.source}: 年份一致`);
  let score =
    0.58 * titleScore +
    0.27 * authorScore +
    (yearScore != null ? 0.1 * yearScore : 0.05) +
    (identifierMatch ? 0.25 : 0.0);
  if (titleScore < 0.6) conflicts.push(`${item.source}: 标题明显不一致`);
  if (entryAuthors(entry).length && item.authors.length && authorScore < 0.25)
    conflicts.push(`${item.source}: 作者明显不一致`);
  if (entryYear(entry) && item.year && yearScore === 0.0)
    conflicts.push(`${item.source}: 年份不一致`);
  const raw = { ...item.raw };
  const ea = entryAuthors(entry);
  raw._expected_first_author_text = ea.length ? ea[0] : "";
  raw._actual_first_author_text = item.authors.length ? item.authors[0] : "";
  return {
    source: item.source,
    title: item.title,
    authors: [...item.authors],
    year: item.year,
    venue: item.venue,
    url: item.url,
    identifier: item.identifier,
    raw,
    score: Math.round(Math.min(1.0, score) * 1000) / 1000,
    title_score: Math.round(titleScore * 1000) / 1000,
    author_score: Math.round(authorScore * 1000) / 1000,
    year_score: yearScore,
    identifier_match: identifierMatch,
    conflicts,
    evidence,
  };
}

function rank(entry, candidates, preferIdentifier = false) {
  const ranked = candidates.map((c) => scoreCandidate(entry, c));
  ranked.sort((a, b) => {
    const ka = [
      preferIdentifier ? (a.identifier_match ? 1 : 0) : 0,
      !preferIdentifier ? (isReliableDiscovery(a) ? 1 : 0) : 0,
      a.score,
      a.title_score,
      a.author_score,
      preferIdentifier && a.source === "arxiv" ? Number((a.raw || {}).arxiv_version || 0) : 0,
    ];
    const kb = [
      preferIdentifier ? (b.identifier_match ? 1 : 0) : 0,
      !preferIdentifier ? (isReliableDiscovery(b) ? 1 : 0) : 0,
      b.score,
      b.title_score,
      b.author_score,
      preferIdentifier && b.source === "arxiv" ? Number((b.raw || {}).arxiv_version || 0) : 0,
    ];
    for (let i = 0; i < ka.length; i++) if (kb[i] !== ka[i]) return kb[i] - ka[i];
    return 0;
  });
  return ranked;
}

function uniqueCandidates(candidates) {
  const result = [];
  const seen = new Set();
  for (const c of candidates) {
    const identity = `${c.source}||${normalizeText(c.title)}||${c.identifier}`;
    if (!seen.has(identity)) {
      seen.add(identity);
      result.push(c);
    }
  }
  return result;
}

// ---- 可靠性判定 ----
function isReliableDiscovery(candidate) {
  if (!candidate) return false;
  return (
    candidate.title_score >= 0.9 &&
    candidate.author_score >= 0.5 &&
    firstAuthorMatch(candidate)
  );
}
function isPlausibleDiscovery(candidate) {
  if (!candidate) return false;
  if (candidate.title_score >= 0.72 && (!candidate.authors.length || candidate.author_score >= 0.25))
    return true;
  return candidate.title_score >= 0.6 && firstAuthorMatch(candidate);
}
function identifierRecordMatches(entry, candidate) {
  return candidate.identifier_match && titlesEqual(entryTitle(entry), candidate.title, candidate);
}
function identifierRecordMismatch(candidate) {
  return candidate.identifier_match && candidate.title_score < 0.9;
}
function identifierIdentitySupported(candidate) {
  return candidate.identifier_match && firstAuthorMatch(candidate) && candidate.author_score >= 0.5;
}

function titleVersionKey(title) {
  const idx = title.indexOf(":");
  if (idx < 0) return "";
  const prefix = title.slice(0, idx);
  const key = normalizeText(prefix);
  if (key.replace(/ /g, "").length < 5) return "";
  if (["analysis", "introduction", "overview", "study", "survey", "towards"].includes(key)) return "";
  return key;
}
function titlesEqual(expected, actual, candidate) {
  if (normalizeText(expected) === normalizeText(actual)) return true;
  if (candidate.title_score >= 0.9) return true;
  return Boolean(
    candidate.identifier_match &&
      candidate.author_score >= 0.5 &&
      firstAuthorMatch(candidate) &&
      titleVersionKey(expected) &&
      titleVersionKey(expected) === titleVersionKey(actual)
  );
}

function titleIsSpecific(title) {
  const tokens = normalizeText(title).split(" ").filter(Boolean);
  if (tokens.length < 8) return false;
  const stop = new Set(["about", "based", "from", "large", "learning", "model", "models", "paper", "study", "using", "with"]);
  const content = tokens.filter((t) => t.length >= 5 && !stop.has(t));
  return new Set(content).size >= 2;
}

function nonacademicReferenceText(entry) {
  return ["url", "howpublished", "note", "repository"]
    .map((n) => String(entry.getField(n) || ""))
    .join(" ")
    .toLowerCase();
}
function entryHasNonacademicReference(entry) {
  const text = nonacademicReferenceText(entry);
  return ["hugging face", "repository", "dataset", "notion.site", "github.com", "blog"].some((m) => text.includes(m));
}
function isNonacademicReference(entry) {
  const text = nonacademicReferenceText(entry);
  return ["notion.site", "notion blog", "blog", "github.com", "repository", "dataset", "hugging face"].some((m) => text.includes(m));
}

function bestIdentifier(candidates) {
  return candidates.find((c) => c.identifier_match) || (candidates.length ? candidates[0] : null);
}
function bestDiscovery(candidates) {
  const reliable = candidates.filter((c) => isReliableDiscovery(c));
  return reliable.length ? reliable[0] : null;
}
function sameRecord(left, right) {
  if (!left || !right) return false;
  if (left.identifier && right.identifier) {
    const ld = doiFromCandidate(left), rd = doiFromCandidate(right);
    if (ld && rd) return normalizeDoi(ld) === normalizeDoi(rd);
    const la = arxivFromCandidate(left), ra = arxivFromCandidate(right);
    if (la && ra) return normalizeArxiv(la) === normalizeArxiv(ra);
  }
  return (
    left.source === right.source &&
    titleSimilarity(left.title, right.title) >= 0.98 &&
    authorSimilarity(left.authors, right.authors) >= 0.95
  );
}

// ---- 字段比对（移植 _compare_fields）----
function compareFields(entry, candidate) {
  if (!candidate) return {};
  const expectedAuthors = entryAuthors(entry);
  const expectedVenue = entryVenue(entry);
  const titleScore = titleSimilarity(entryTitle(entry), candidate.title);
  let retrievedVenue = candidate.venue;
  if ((candidate.source === "arxiv" || candidate.source === "datacite") && venuesEqual(candidate.venue, "arXiv"))
    retrievedVenue = "";
  const teq = titlesEqual(entryTitle(entry), candidate.title, candidate);
  const values = {
    title: [entryTitle(entry), candidate.title, teq],
    authors: compareAuthors(expectedAuthors, candidate.authors),
    year: [entryYear(entry), candidate.year, yearSimilarity(entryYear(entry), candidate.year) === 1.0],
    venue: [expectedVenue, retrievedVenue, venuesEqual(expectedVenue, retrievedVenue)],
    doi: [entryDoi(entry), doiFromCandidate(candidate), identifierEqual(entryDoi(entry), doiFromCandidate(candidate), "doi")],
    arxiv_id: [entryArxiv(entry), arxivFromCandidate(candidate), identifierEqual(entryArxiv(entry), arxivFromCandidate(candidate), "arxiv")],
  };
  const result = {};
  for (const [name, value] of Object.entries(values)) {
    if (name === "authors") {
      result[name] = value;
      continue;
    }
    const [expected, actual, equal] = value;
    const empty = (v) => v === "" || v == null || (Array.isArray(v) && v.length === 0);
    let status;
    if (empty(expected) || empty(actual)) status = "not_available";
    else if (name === "title" && !equal && titleScore >= 0.72) status = "compatible";
    else status = equal ? "match" : "mismatch";
    result[name] = { bibtex: expected, retrieved: actual, status };
  }
  return result;
}

function hasFieldIssue(comparison) {
  return Object.entries(comparison).some(
    ([field, item]) =>
      ["mismatch", "minor_difference", "major_mismatch"].includes(item.status) ||
      (field === "title" && item.status === "compatible")
  );
}

function authorDiffSummary(item) {
  const parts = ["authors:"];
  if (item.added && item.added.length) parts.push("新增 " + item.added.join("；"));
  if (item.removed && item.removed.length) parts.push("删除 " + item.removed.join("；"));
  if (item.reordered && item.reordered.length) parts.push("顺序调整 " + item.reordered.join("；"));
  if (parts.length === 1) parts.push("作者列表存在差异");
  return parts.join(" ");
}

// 复刻 Python repr()：字符串用单引号包裹（内部单引号转义），数字/None 裸值。
function pyRepr(value) {
  if (value == null) return "None";
  if (typeof value === "number") return String(value);
  const s = String(value);
  return "'" + s.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
}

function comparisonReasons(comparison) {
  const output = [];
  for (const [field, item] of Object.entries(comparison)) {
    const flagged =
      ["mismatch", "minor_difference", "major_mismatch"].includes(item.status) ||
      (field === "title" && item.status === "compatible");
    if (!flagged) continue;
    if (field === "authors") {
      output.push(authorDiffSummary(item));
      continue;
    }
    const label = FIELD_LABELS[field] || field;
    output.push(`${label}：Bib=${pyRepr(item.bibtex)}；检索=${pyRepr(item.retrieved)}`);
  }
  return output.length ? output : ["可比字段没有发现明确差异"];
}

// ---- 分类（移植 _classify）----
// authoritativeMisses / noMatchSources 等在纯前端下由 engine 传入（多为 0）。
function classify(ctx) {
  const {
    entry,
    bestIdentifier: bi,
    bestDiscovery: bd,
    discoveryCandidateCount,
    hasDiscoveryCandidates,
    noMatchSources,
    authoritativeMisses,
    identifierSuccess,
    completed,
    providerErrors,
    fieldComparison,
  } = ctx;

  if (!entryTitle(entry) && !(entryDoi(entry) || entryArxiv(entry)))
    return [UNCONFIRMED, 0.0, ["条目缺少标题、DOI 或 arXiv ID"]];

  const hasErrors = Object.keys(providerErrors).length > 0;

  if (bi) {
    if (bi.identifier_match) {
      if (isReliableDiscovery(bd) && !sameRecord(bi, bd))
        return [NEEDS_REVIEW, bi.score, [
          "标识符指向另一篇论文，但标题和作者检索到了疑似目标论文",
          ...comparisonReasons(compareFields(entry, bd)),
          `标识符记录：${bi.source} - ${bi.title}`,
        ]];
      if (identifierRecordMatches(entry, bi))
        return [
          hasFieldIssue(fieldComparison) ? NEEDS_REVIEW : VALIDATED,
          bi.score,
          hasFieldIssue(fieldComparison)
            ? ["标识符和标题对应真实论文，但存在字段差异", ...comparisonReasons(fieldComparison)]
            : ["标识符、标题和作者对应真实论文"],
        ];
      if (identifierRecordMismatch(bi)) {
        if (isReliableDiscovery(bd))
          return [NEEDS_REVIEW, bi.score, [
            "标识符指向另一篇论文，但标题搜索找到了疑似目标论文",
            ...comparisonReasons(fieldComparison),
            `疑似目标：${bd.source} - ${bd.title}`,
          ]];
        if (identifierIdentitySupported(bi))
          return [NEEDS_REVIEW, bi.score, ["标识符对应真实论文，但标题可能是不同版本的写法", ...comparisonReasons(fieldComparison)]];
        if (bi.title_score < 0.75 && !identifierIdentitySupported(bi))
          return [LIKELY_HALLUCINATION, bi.score, ["标识符指向的论文与 Bib 的标题明显不一致，且标题/作者检索未找到目标", ...comparisonReasons(fieldComparison)]];
        return [UNCONFIRMED, bi.score, ["标识符记录与 Bib 的标题仅部分相似，尚不足以确认是同一篇论文", ...comparisonReasons(fieldComparison)]];
      }
      if (hasFieldIssue(fieldComparison))
        return [NEEDS_REVIEW, bi.score, ["标识符对应真实论文，但存在字段差异", ...comparisonReasons(fieldComparison)]];
      return [VALIDATED, bi.score, ["标识符、标题、作者、年份和可用出版信息均一致"]];
    }
    if (isReliableDiscovery(bd))
      return [NEEDS_REVIEW, bd.score, ["标题搜索找到目标论文，但 Bib 中的标识符未对应", ...comparisonReasons(compareFields(entry, bd))]];
    if (identifierSuccess && !hasErrors)
      return [LIKELY_HALLUCINATION, bi.score, ["标识符查询返回了记录，但记录与 Bib 不匹配", ...comparisonReasons(fieldComparison)]];
    return [UNCONFIRMED, bi.score, ["未能把 Bib 标识符与可信论文对应", ...comparisonReasons(fieldComparison)]];
  }

  if (isReliableDiscovery(bd)) {
    const comparison = compareFields(entry, bd);
    if (isNonacademicReference(entry)) {
      if (hasFieldIssue(comparison))
        return [NEEDS_REVIEW, bd.score, ["找到可信网页/博客，但存在字段差异", ...comparisonReasons(comparison)]];
      return [VALIDATED, bd.score, ["标题、作者和年份对应真实网页/博客"]];
    }
    if (hasFieldIssue(comparison))
      return [NEEDS_REVIEW, bd.score, ["找到可信论文，但存在字段差异", ...comparisonReasons(comparison)]];
    return [VALIDATED, bd.score, ["标题、作者、年份和可用出版信息均一致"]];
  }

  if (authoritativeMisses && !entryHasNonacademicReference(entry))
    return [LIKELY_HALLUCINATION, 0.0, [`${authoritativeMisses} 个对应会议/期刊的官方数据源在官方检索范围内未找到可信标题匹配`]];

  const noCloseCandidate = discoveryCandidateCount === 0;
  if (
    noCloseCandidate &&
    titleIsSpecific(entryTitle(entry)) &&
    !entryHasNonacademicReference(entry) &&
    (noMatchSources >= 3 ||
      (completed >= 2 && noMatchSources >= 2 && (!hasDiscoveryCandidates || entryAuthors(entry).length >= 3)))
  )
    return [LIKELY_HALLUCINATION, 0.0, [`${noMatchSources} 个已完成的独立学术数据源均未找到可信标题匹配`]];

  return [UNCONFIRMED, 0.0, ["检索结果不足以确认或否定该引用", "没有获得标题和作者同时可靠匹配的记录"]];
}

// finish：把候选与统计整合出 CheckResult 形状（与 CheckResult.as_dict 对齐）。
export function finishCheck({
  entry,
  identifierCandidates,
  discoveryCandidates,
  providerErrors,
  completed,
  noMatchSources,
  authoritativeMisses,
  identifierSuccess,
}) {
  const rankedIdentifiers = rank(entry, identifierCandidates, true);
  const rankedDiscovery = rank(entry, discoveryCandidates, false);
  const ranked = uniqueCandidates([...rankedIdentifiers, ...rankedDiscovery]);
  const bi = bestIdentifier(rankedIdentifiers);
  const bd = bestDiscovery(rankedDiscovery);
  const comparisonCandidate =
    bi ||
    bd ||
    (rankedDiscovery.length && isPlausibleDiscovery(rankedDiscovery[0]) ? rankedDiscovery[0] : null);
  const fieldComparison = comparisonCandidate ? compareFields(entry, comparisonCandidate) : {};

  const [status, score, reasons] = classify({
    entry,
    bestIdentifier: bi,
    bestDiscovery: bd,
    discoveryCandidateCount: rankedDiscovery.filter((i) => isPlausibleDiscovery(i)).length,
    hasDiscoveryCandidates: discoveryCandidates.length > 0,
    noMatchSources,
    authoritativeMisses,
    identifierSuccess,
    completed,
    providerErrors,
    fieldComparison,
  });

  let errors = providerErrors;
  if (status !== UNCONFIRMED && (ranked.length || authoritativeMisses)) errors = {};

  return {
    key: entryKey(entry),
    status,
    classification: status,
    reasons,
    evidence: reasons,
    score: Math.round(score * 1000) / 1000,
    provider_errors: errors,
    field_comparison: fieldComparison,
    candidates: ranked.slice(0, 5).map((c) => ({
      source: c.source,
      title: c.title,
      authors: c.authors,
      year: c.year,
      venue: c.venue,
      url: c.url,
      identifier: c.identifier,
      score: c.score,
      title_score: c.title_score,
      author_score: c.author_score,
      year_score: c.year_score,
      identifier_match: c.identifier_match,
      conflicts: c.conflicts,
      evidence: c.evidence,
    })),
  };
}

export { entryTitle, entryAuthors, entryYear, entryDoi, entryArxiv, titleSimilarity };
