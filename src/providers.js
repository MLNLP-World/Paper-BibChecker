// OpenAlex 与 Crossref 数据源（浏览器 fetch）。移植 providers.py 中这两个类的
// 查询构造与 JSON→Candidate 解析。二者都允许浏览器跨域（CORS），可纯前端调用。
import { Candidate } from "./models.js";

const DOI_RE = /10\.\d{4,9}\/[-._;()/:A-Z0-9]+/i;
const ARXIV_RE = /(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?/i;

function extractDoi(...values) {
  for (const v of values) {
    const m = DOI_RE.exec(String(v || ""));
    if (m) return m[0].replace(/[.,;)]+$/, "").toLowerCase();
  }
  return "";
}
function extractArxiv(...values) {
  for (const v of values) {
    const m = ARXIV_RE.exec(String(v || ""));
    if (m) return m[1].toLowerCase();
  }
  return "";
}
function yearOf(value) {
  const m = /(?:18|19|20|21)\d{2}/.exec(String(value == null ? "" : value));
  return m ? parseInt(m[0], 10) : null;
}
function normalizeDoi(value) {
  return value.trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "").toLowerCase();
}

async function getJson(url, { email, signal } = {}) {
  const headers = { Accept: "application/json" };
  const resp = await fetch(url, { headers, signal });
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ---- 条目字段访问（BibEntry） ----
function eTitle(e) {
  return String(e.getField("title") || "");
}
function eDoi(e) {
  return String(e.getField("doi") || "");
}
function eFirstAuthor(e) {
  const authors = e.getField("authors", "author") || [];
  let list = authors;
  if (typeof authors === "string") list = authors.split(/\s+and\s+/i);
  if (!list.length) return "";
  let author = String(list[0]).trim();
  if (author.includes(",")) return author.split(",")[0].trim();
  const parts = author.split(/\s+/);
  return parts.length ? parts[parts.length - 1] : "";
}

// =================== OpenAlex ===================
const OPENALEX = "https://api.openalex.org/works";
const OA_SELECT = "id,display_name,publication_year,authorships,doi,ids,primary_location,locations,type";

function openAlexCandidates(records, title) {
  const candidates = [];
  for (const item of records) {
    const location = item.primary_location || {};
    const source = location.source || {};
    const authors = (item.authorships || []).map(
      (a) => a.raw_author_name || (a.author || {}).display_name || ""
    );
    const ids = item.ids || {};
    const doiValue = extractDoi(item.doi, ids.doi);
    const arxivId = extractArxiv(
      ids.arxiv,
      location.landing_page_url,
      ...(item.locations || []).map((l) => l.landing_page_url)
    );
    candidates.push(
      new Candidate({
        source: "openalex",
        title: item.display_name || item.title || "",
        authors: authors.filter(Boolean),
        year: yearOf(item.publication_year),
        venue: source.display_name || "",
        url: location.landing_page_url || item.doi || item.id || "",
        identifier: doiValue || arxivId || "",
        raw: { doi: doiValue, arxiv_id: arxivId },
      })
    );
  }
  return sortByTitle(title, candidates);
}

async function openAlexIdentifier(entry, opts) {
  const doi = eDoi(entry);
  if (!doi) return null;
  const url = `${OPENALEX}/https://doi.org/${encodeURIComponent(normalizeDoi(doi))}`;
  const record = await getJson(url, opts);
  return openAlexCandidates(record ? [record] : [], eTitle(entry));
}

async function openAlexTitle(entry, opts) {
  const title = eTitle(entry);
  if (!title) return [];
  const params = new URLSearchParams({ search: title, "per-page": "5", select: OA_SELECT });
  if (opts.email) params.set("mailto", opts.email);
  const data = await getJson(`${OPENALEX}?${params}`, opts);
  return openAlexCandidates((data && data.results) || [], title);
}

// =================== Crossref ===================
const CROSSREF = "https://api.crossref.org/works";
const CR_SELECT = "DOI,title,author,published,published-print,published-online,URL,container-title";

function crossrefYear(item) {
  for (const name of ["published-print", "published-online", "published", "issued"]) {
    const parts = ((item[name] || {})["date-parts"]) || [];
    if (parts.length && parts[0] && parts[0].length) return yearOf(parts[0][0]);
  }
  return null;
}

function crossrefCandidates(records) {
  const candidates = [];
  for (const item of records) {
    const titleValue = item.title || [""];
    const container = item["container-title"] || [""];
    const authors = (item.author || []).map((a) =>
      [a.given, a.family].filter(Boolean).join(" ")
    );
    const doiValue = extractDoi(item.DOI);
    candidates.push(
      new Candidate({
        source: "crossref",
        title: Array.isArray(titleValue) ? titleValue[0] : String(titleValue),
        authors: authors.filter(Boolean),
        year: crossrefYear(item),
        venue: Array.isArray(container) ? container[0] : String(container),
        url: item.URL || "",
        identifier: doiValue || extractArxiv(item.URL) || "",
        raw: { doi: doiValue },
      })
    );
  }
  return candidates;
}

async function crossrefIdentifier(entry, opts) {
  const doi = eDoi(entry);
  if (!doi) return null;
  const url = `${CROSSREF}/${encodeURIComponent(normalizeDoi(doi))}`;
  const data = await getJson(url, opts);
  return crossrefCandidates(data ? [data.message || {}] : []);
}

async function crossrefTitle(entry, opts) {
  const title = eTitle(entry);
  if (!title) return [];
  const params = new URLSearchParams({ "query.bibliographic": title, rows: "5", select: CR_SELECT });
  const firstAuthor = eFirstAuthor(entry);
  if (firstAuthor) params.set("query.author", firstAuthor);
  if (opts.email) params.set("mailto", opts.email);
  const data = await getJson(`${CROSSREF}?${params}`, opts);
  const items = ((data && data.message) || {}).items || [];
  return crossrefCandidates(items);
}

// ---- 标题排序（移植 _sort_title_candidates）----
function titleKey(value) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
}
function titleOverlap(left, right) {
  if (!left || !right) return 0.0;
  const l = new Set(left.split(" "));
  const r = new Set(right.split(" "));
  let overlap = 0;
  for (const t of l) if (r.has(t)) overlap++;
  return (2 * overlap) / (l.size + r.size);
}
function sortByTitle(title, candidates) {
  const expected = titleKey(title);
  return [...candidates].sort((a, b) => {
    const ka = [titleKey(a.title) === expected ? 1 : 0, titleOverlap(expected, titleKey(a.title))];
    const kb = [titleKey(b.title) === expected ? 1 : 0, titleOverlap(expected, titleKey(b.title))];
    for (let i = 0; i < ka.length; i++) if (kb[i] !== ka[i]) return kb[i] - ka[i];
    return 0;
  });
}

export const PROVIDERS = {
  openalex: { identifier: openAlexIdentifier, title: openAlexTitle, academic: true },
  crossref: { identifier: crossrefIdentifier, title: crossrefTitle, academic: true },
};
