// 作者比对与相似度。移植 bibchecker/checker.py 中自带的作者函数
// （注意：check_entry 走的是 checker.py 自身的实现，不是 matching.py）。
import { normalizeText } from "./latex.js";

const TRUNCATION_MARKERS = new Set(["others", "etal", "andothers"]);

export function isTruncationMarker(author) {
  return TRUNCATION_MARKERS.has(normalizeText(author).replace(/ /g, ""));
}

function authorIdentity(author) {
  const value = normalizeText(author);
  if (!value) return ["", ""];
  if (author.includes(",")) {
    const family = author.split(",")[0];
    return [normalizeText(family), givenInitials(author.split(",").slice(1).join(","))];
  }
  const tokens = value.split(" ");
  return [tokens[tokens.length - 1], givenInitials(tokens.slice(0, -1).join(" "))];
}

function givenInitials(value) {
  return normalizeText(value)
    .split(" ")
    .filter(Boolean)
    .map((t) => t[0])
    .join("");
}

export function authorsMatch(left, right) {
  const [leftFamily, leftInitials] = authorIdentity(left);
  const [rightFamily, rightInitials] = authorIdentity(right);
  if (!leftFamily || !rightFamily) return false;
  const familyMatch =
    leftFamily === rightFamily ||
    leftFamily.split(" ").pop() === rightFamily.split(" ").pop();
  if (!familyMatch) return false;
  if (!leftInitials || !rightInitials) return true;
  return (
    leftInitials === rightInitials ||
    leftInitials.startsWith(rightInitials) ||
    rightInitials.startsWith(leftInitials)
  );
}

// 贪心逐一匹配（checker.py 的 _match_authors：对每个 expected 取第一个未用的匹配 actual）。
export function matchAuthors(expected, actual) {
  const pairs = [];
  const usedActual = new Set();
  for (let ei = 0; ei < expected.length; ei++) {
    for (let ai = 0; ai < actual.length; ai++) {
      if (usedActual.has(ai)) continue;
      if (authorsMatch(expected[ei], actual[ai])) {
        pairs.push([ei, ai]);
        usedActual.add(ai);
        break;
      }
    }
  }
  return pairs;
}

export function displayAuthor(author) {
  let value = author; // checker.py 用 _decode_latex；这里显示用途，简化保留
  value = value.replace(/[{}]/g, "");
  value = value.split(/\s+/).filter(Boolean).join(" ").trim();
  if (value.includes(",")) {
    const [family, given] = value.split(",").map((p) => p.trim());
    return [given, family].filter(Boolean).join(" ");
  }
  return value;
}

function reorderedAuthors(pairs, actual) {
  if (pairs.length < 2) return [];
  const orderedPairs = [...pairs].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const actualPositions = orderedPairs.map(([, ai]) => ai);
  const sorted = [...actualPositions].sort((a, b) => a - b);
  if (JSON.stringify(actualPositions) === JSON.stringify(sorted)) return [];
  const rankByActual = new Map();
  sorted.forEach((ai, rank) => rankByActual.set(ai, rank));
  const out = [];
  orderedPairs.forEach(([ei, ai], expectedRank) => {
    if (rankByActual.get(ai) !== expectedRank)
      out.push(`${displayAuthor(actual[ai])}（Bib第${ei + 1}位→检索第${ai + 1}位）`);
  });
  return out;
}

// 移植 checker.py 的 _compare_authors。
export function compareAuthors(expected, actual) {
  const expectedNames = expected.filter((a) => !isTruncationMarker(a));
  const actualNames = actual.filter((a) => !isTruncationMarker(a));
  if (!expectedNames.length || !actualNames.length) {
    return {
      bibtex: [...expected],
      retrieved: [...actual],
      status: "not_available",
      added: [],
      removed: [],
      reordered: [],
    };
  }
  const truncated = expected.some((a) => isTruncationMarker(a));
  const pairs = matchAuthors(expectedNames, actualNames);
  const expectedMatched = new Set(pairs.map(([ei]) => ei));
  const actualMatched = new Set(pairs.map(([, ai]) => ai));
  let added = [];
  let removed = [];
  let reordered = [];
  for (let i = 0; i < actualNames.length; i++)
    if (!actualMatched.has(i))
      added.push(`${displayAuthor(actualNames[i])}（检索第${i + 1}位）`);
  for (let i = 0; i < expectedNames.length; i++)
    if (!expectedMatched.has(i))
      removed.push(`${displayAuthor(expectedNames[i])}（Bib第${i + 1}位）`);
  reordered = reorderedAuthors(pairs, actualNames);
  const overlap = pairs.length / Math.max(expectedNames.length, actualNames.length);

  let status;
  if (truncated) {
    status = pairs.length ? "match" : "major_mismatch";
    added = [];
    removed = [];
    reordered = [];
  } else if (!added.length && !removed.length && !reordered.length) {
    status = "match";
  } else if (overlap >= 0.6) {
    status = "minor_difference";
  } else {
    status = "major_mismatch";
  }
  return {
    bibtex: [...expected],
    retrieved: [...actual],
    status,
    added,
    removed,
    reordered,
    overlap: Math.round(overlap * 1000) / 1000,
  };
}

// 移植 checker.py 的 _author_similarity。
export function authorSimilarity(expected, actual) {
  const left = expected.filter((a) => !isTruncationMarker(a));
  const right = actual.filter((a) => !isTruncationMarker(a));
  if (!left.length || !right.length) return 0.0;
  const pairs = matchAuthors(left, right);
  if (expected.some((a) => isTruncationMarker(a)) && pairs.length === left.length)
    return 1.0;
  const matches = pairs.length;
  if (!matches) return 0.0;
  const precision = matches / right.length;
  const recall = matches / left.length;
  return (2 * precision * recall) / (precision + recall);
}
