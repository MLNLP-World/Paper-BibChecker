// 数据模型。移植 bibchecker/models.py 的 BibEntry 派生属性与 Candidate。

export class BibEntry {
  constructor(key, entryType, fields) {
    this.key = String(key).trim();
    this.entryType = String(entryType).trim().toLowerCase();
    this.fields = {};
    for (const [name, value] of Object.entries(fields || {})) {
      this.fields[String(name).trim().toLowerCase()] = String(value).trim();
    }
  }

  get title() {
    return this.fields.title || "";
  }

  get author() {
    return this.fields.author || "";
  }

  // 按 BibTeX 顶层的 " and " 分隔符拆分作者（对应 models.py 的 authors 属性）。
  get authors() {
    const value = this.author;
    const authors = [];
    let start = 0;
    let depth = 0;
    let index = 0;
    while (index < value.length) {
      const char = value[index];
      if (char === "\\") {
        index += 2;
        continue;
      }
      if (char === "{") depth++;
      else if (char === "}" && depth) depth--;
      else if (
        depth === 0 &&
        value.slice(index, index + 3).toLowerCase() === "and" &&
        (index === 0 || /\s/.test(value[index - 1])) &&
        (index + 3 === value.length || /\s/.test(value[index + 3]))
      ) {
        const author = value.slice(start, index).trim();
        if (author) authors.push(author);
        start = index + 3;
        index += 3;
        continue;
      }
      index++;
    }
    const author = value.slice(start).trim();
    if (author) authors.push(author);
    return authors;
  }

  get year() {
    const value = this.fields.year || "";
    const digits = value.replace(/\D/g, "");
    return digits.length >= 4 ? parseInt(digits.slice(0, 4), 10) : null;
  }

  get doi() {
    const value = (this.fields.doi || "").trim();
    return value
      .replace(/^(?:doi\s*:\s*|https?:\/\/(?:dx\.)?doi\.org\/)/i, "")
      .replace(/\.+$/, "");
  }

  get arxiv_id() {
    const identifierRe = /(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*\/\d{7})(?:v\d+)?/i;
    for (const field of ["eprint", "arxiv", "arxivid", "url", "doi", "journal", "howpublished", "note"]) {
      let value = (this.fields[field] || "").trim();
      if (!value) continue;
      value = value.replace(/^https?:\/\/arxiv\.org\/(?:abs|pdf)\//i, "");
      value = value.replace(/^arxiv\s*:\s*/i, "");
      value = value.split("?")[0].replace(/\.pdf$/, "");
      const match = identifierRe.exec(value);
      if (match) return match[0];
    }
    return "";
  }

  // 供 _value(entry, name) 使用的字段访问（对应 Python 对 dataclass 的属性/fields 双查）。
  getField(...names) {
    for (const name of names) {
      const lower = name.toLowerCase();
      // 派生属性优先（authors/year/doi/arxiv_id/title）
      if (lower === "title") return this.title;
      if (lower === "authors") return this.authors;
      if (lower === "author") return this.author;
      if (lower === "year") return this.year;
      if (lower === "doi") return this.doi;
      if (lower === "arxiv_id" || lower === "arxiv" || lower === "eprint") {
        const v = this.arxiv_id;
        if (v) return v;
      }
      if (this.fields[lower] != null) return this.fields[lower];
    }
    return null;
  }
}

export class Candidate {
  constructor({
    source = "",
    title = "",
    authors = [],
    year = null,
    venue = "",
    url = "",
    identifier = "",
    raw = {},
  } = {}) {
    this.source = source;
    this.title = title;
    this.authors = authors;
    this.year = year;
    this.venue = venue;
    this.url = url;
    this.identifier = identifier;
    this.raw = raw;
  }
}
