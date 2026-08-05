// BibTeX 解析（不依赖第三方库）。移植 bibchecker/parser.py。
// 浏览器版直接接收文本字符串（而非文件路径）。
import { BibEntry } from "./models.js";

const IGNORED_BIB_BLOCKS = new Set(["comment", "preamble", "string"]);

function isEscaped(text, position) {
  let backslashes = 0;
  let i = position - 1;
  while (i >= 0 && text[i] === "\\") {
    backslashes++;
    i--;
  }
  return backslashes % 2 === 1;
}

function stripPercentComments(text) {
  const out = [];
  let index = 0;
  while (index < text.length) {
    if (text[index] === "%" && !isEscaped(text, index)) {
      const newline = text.indexOf("\n", index);
      if (newline < 0) break;
      out.push("\n");
      index = newline + 1;
      continue;
    }
    out.push(text[index]);
    index++;
  }
  return out.join("");
}

function findBibBlockEnd(text, start, opener, closer) {
  let outerDepth = 1;
  let braceDepth = 0;
  let quoted = false;
  let index = start + 1;
  while (index < text.length) {
    const char = text[index];
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (char === '"') {
      quoted = !quoted;
      index++;
      continue;
    }
    if (quoted) {
      index++;
      continue;
    }
    if (opener === "{") {
      if (char === "{") outerDepth++;
      else if (char === "}") {
        outerDepth--;
        if (outerDepth === 0) return index;
      }
    } else {
      if (char === "{") braceDepth++;
      else if (char === "}" && braceDepth) braceDepth--;
      else if (braceDepth === 0) {
        if (char === "(") outerDepth++;
        else if (char === ")") {
          outerDepth--;
          if (outerDepth === 0) return index;
        }
      }
    }
    index++;
  }
  return null;
}

function* iterBibBlocks(text) {
  let index = 0;
  while (index < text.length) {
    const marker = text.indexOf("@", index);
    if (marker < 0) return;
    let nameStart = marker + 1;
    while (nameStart < text.length && /\s/.test(text[nameStart])) nameStart++;
    let nameEnd = nameStart;
    while (
      nameEnd < text.length &&
      (/[a-zA-Z0-9]/.test(text[nameEnd]) || text[nameEnd] === "_" || text[nameEnd] === "-")
    )
      nameEnd++;
    const entryType = text.slice(nameStart, nameEnd).toLowerCase();
    let openerIndex = nameEnd;
    while (openerIndex < text.length && /\s/.test(text[openerIndex])) openerIndex++;
    if (!entryType || openerIndex >= text.length) {
      index = marker + 1;
      continue;
    }
    const opener = text[openerIndex];
    if (opener !== "{" && opener !== "(") {
      index = marker + 1;
      continue;
    }
    const closer = opener === "{" ? "}" : ")";
    const end = findBibBlockEnd(text, openerIndex, opener, closer);
    if (end == null) return;
    yield [entryType, text.slice(openerIndex + 1, end)];
    index = end + 1;
  }
}

// 移植 _split_top_level：按 separator 在顶层（不在 {} 或 "" 内）切分。
function splitTopLevel(text, separator, maxsplit = -1) {
  const parts = [];
  let start = 0;
  let depth = 0;
  let quoted = false;
  let splits = 0;
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (char === '"') {
      quoted = !quoted;
    } else if (!quoted) {
      if (char === "{") depth++;
      else if (char === "}" && depth) depth--;
      else if (
        char === separator &&
        depth === 0 &&
        (maxsplit < 0 || splits < maxsplit)
      ) {
        parts.push(text.slice(start, index));
        start = index + 1;
        splits++;
      }
    }
    index++;
  }
  parts.push(text.slice(start));
  return parts;
}

function splitEntryContent(content) {
  const parts = splitTopLevel(content, ",", 1);
  return [parts[0] ? parts[0].trim() : "", parts.length === 2 ? parts[1] : ""];
}

function splitAssignment(assignment) {
  const parts = splitTopLevel(assignment, "=", 1);
  if (parts.length !== 2 || !parts[0].trim()) return [null, ""];
  return [parts[0].trim(), parts[1].trim()];
}

function hasOuterDelimiters(value, opener, closer) {
  if (value.length < 2 || value[0] !== opener || value[value.length - 1] !== closer)
    return false;
  if (opener === '"') return !isEscaped(value, value.length - 1);
  let depth = 0;
  for (let index = 0; index < value.length; index++) {
    const char = value[index];
    if (char === "\\") continue;
    if (char === opener) depth++;
    else if (char === closer) {
      depth--;
      if (depth === 0 && index !== value.length - 1) return false;
    }
  }
  return depth === 0;
}

function parseValue(expression, resolveMacro) {
  const values = [];
  for (const part of splitTopLevel(expression.trim(), "#")) {
    const value = part.trim();
    if (!value) continue;
    if (hasOuterDelimiters(value, "{", "}")) values.push(value.slice(1, -1));
    else if (hasOuterDelimiters(value, '"', '"')) values.push(value.slice(1, -1));
    else values.push(resolveMacro(value) || value);
  }
  return values.join("").replace(/\s+/g, " ").trim();
}

export function parseBib(text) {
  const cleaned = stripPercentComments(text);
  const blocks = [...iterBibBlocks(cleaned)];

  const rawMacros = new Map();
  for (const [entryType, content] of blocks) {
    if (entryType !== "string") continue;
    for (const assignment of splitTopLevel(content, ",")) {
      const [name, expression] = splitAssignment(assignment);
      if (name != null) rawMacros.set(name.toLowerCase(), expression);
    }
  }

  const resolvedMacros = new Map();
  function resolveMacro(name, resolving) {
    const normalized = name.toLowerCase();
    if (resolvedMacros.has(normalized)) return resolvedMacros.get(normalized);
    if (!rawMacros.has(normalized)) return null;
    resolving = resolving || new Set();
    if (resolving.has(normalized)) return name;
    resolving.add(normalized);
    const value = parseValue(rawMacros.get(normalized), (item) =>
      resolveMacro(item, resolving)
    );
    resolving.delete(normalized);
    resolvedMacros.set(normalized, value);
    return value;
  }

  const entries = new Map();
  for (const [entryType, content] of blocks) {
    if (IGNORED_BIB_BLOCKS.has(entryType)) continue;
    const [key, fieldText] = splitEntryContent(content);
    if (!key) continue;
    const fields = {};
    for (const assignment of splitTopLevel(fieldText, ",")) {
      const [name, expression] = splitAssignment(assignment);
      if (name != null) fields[name.toLowerCase()] = parseValue(expression, (n) => resolveMacro(n));
    }
    entries.set(key, new BibEntry(key, entryType, fields));
  }
  return entries;
}

// ---- LaTeX 引用键提取（移植 parser.py 的 find_citation_keys / _scan_latex）----

const CITATION_COMMANDS = new Set([
  "autocite", "autocites", "cite", "citealp", "citealt", "citeauthor",
  "citedate", "citefield", "citefullauthor", "citelabel", "citep", "cites",
  "citet", "citetext", "citetitle", "citeurl", "citeyear", "citeyearpar",
  "footcite", "footcites", "footfullcite", "fullcite", "headlesscite",
  "nocite", "notecite", "onlinecite", "parencite", "parencites", "smartcite",
  "smartcites", "supercite", "textcite", "textcites",
]);
const MULTI_CITATION_COMMANDS = new Set(
  [...CITATION_COMMANDS].filter((c) => c.endsWith("cites"))
);

const COMMAND_RE = /\\([A-Za-z@]+)/g;
const VERBATIM_ENV_RE =
  /\\begin\s*\{(verbatim\*?|Verbatim|lstlisting|minted|comment)\}[\s\S]*?\\end\s*\{\1\}/g;

function skipSpace(text, position) {
  while (position < text.length && /\s/.test(text[position])) position++;
  return position;
}

function readGroup(text, position, opener, closer) {
  if (position >= text.length || text[position] !== opener) return null;
  let depth = 1;
  let index = position + 1;
  while (index < text.length) {
    const char = text[index];
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (char === opener) depth++;
    else if (char === closer) {
      depth--;
      if (depth === 0) return [text.slice(position + 1, index), index + 1];
    }
    index++;
  }
  return null;
}

function citationArguments(text, position, multiple) {
  position = skipSpace(text, position);
  if (position < text.length && text[position] === "*")
    position = skipSpace(text, position + 1);
  const args = [];
  for (;;) {
    while (position < text.length && text[position] === "[") {
      const group = readGroup(text, position, "[", "]");
      if (group == null) return args;
      position = group[1];
      position = skipSpace(text, position);
    }
    if (position >= text.length || text[position] !== "{") return args;
    const group = readGroup(text, position, "{", "}");
    if (group == null) return args;
    args.push(group[0]);
    position = group[1];
    if (!multiple) return args;
    position = skipSpace(text, position);
  }
}

function includeArgument(text, position) {
  position = skipSpace(text, position);
  if (position >= text.length) return null;
  if (text[position] === "{") {
    const group = readGroup(text, position, "{", "}");
    return group ? group[0] : null;
  }
  let end = position;
  while (end < text.length && !/\s/.test(text[end]) && text[end] !== "{" && text[end] !== "}")
    end++;
  return text.slice(position, end) || null;
}

function maskVerbatim(text) {
  text = text.replace(VERBATIM_ENV_RE, (m) => "\n".repeat((m.match(/\n/g) || []).length));
  const chars = [...text];
  const verbRe = /\\verb\*?/g;
  let m;
  while ((m = verbRe.exec(text)) !== null) {
    const delimiterIndex = m.index + m[0].length;
    if (delimiterIndex >= text.length) continue;
    const delimiter = text[delimiterIndex];
    if (/\s/.test(delimiter) || /[a-zA-Z0-9]/.test(delimiter)) continue;
    const end = text.indexOf(delimiter, delimiterIndex + 1);
    if (end < 0) continue;
    for (let i = m.index; i <= end; i++) if (chars[i] !== "\n") chars[i] = " ";
  }
  return chars.join("");
}

function scanLatex(text) {
  text = maskVerbatim(stripPercentComments(text));
  const citations = new Set();
  const includes = [];
  let match;
  COMMAND_RE.lastIndex = 0;
  while ((match = COMMAND_RE.exec(text)) !== null) {
    if (isEscaped(text, match.index)) continue;
    const command = match[1].toLowerCase();
    const end = match.index + match[0].length;
    if (CITATION_COMMANDS.has(command)) {
      const args = citationArguments(text, end, MULTI_CITATION_COMMANDS.has(command));
      for (const argument of args)
        for (const key of argument.split(",").map((s) => s.trim()))
          if (key) citations.add(key);
    } else if (command === "input" || command === "include") {
      const argument = includeArgument(text, end);
      if (argument)
        for (const item of argument.split(",").map((s) => s.trim()))
          if (item) includes.push(item);
    }
  }
  return { citations, includes };
}

// 浏览器版：无文件系统，无法递归 \input。texFiles 是 {name: content} 映射；
// 顶层文件的 \input 若能在映射中找到就递归，否则忽略（缺失即忽略，与 CLI 对
// 缺失 include 的宽松处理一致）。多数用户只传一个根 .tex，够用。
export function findCitationKeys(texFiles) {
  const citations = new Set();
  const visited = new Set();
  function visit(name) {
    if (visited.has(name)) return;
    const content = texFiles[name];
    if (content == null) return;
    visited.add(name);
    const { citations: c, includes } = scanLatex(content);
    for (const key of c) citations.add(key);
    for (const inc of includes) {
      const candidate = texFiles[inc] != null ? inc : `${inc}.tex`;
      visit(candidate);
    }
  }
  for (const name of Object.keys(texFiles)) visit(name);
  return citations;
}
