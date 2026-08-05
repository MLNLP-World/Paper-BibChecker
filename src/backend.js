// 后端 fallback：把前端条目序列化后发送到 Render 后端的 /api/check-entry 做深度检查。
// 后端走的 providers 比前端多（arXiv、DBLP、ICLR/NeurIPS/ICML/CVPR/ACL/EMNLP/ECCV
// 官方论文集、JMLR、TACL、OpenReview 等），能给出更准确的官方数据结论。

export function createBackendFallback(backendUrl) {
  if (!backendUrl) return null;

  return async function fallback(entry, key, { signal } = {}) {
    const fields = {};
    for (const [name, value] of Object.entries(entry.fields)) {
      fields[name] = String(value);
    }

    const resp = await fetch(backendUrl + "/api/check-entry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fields,
        entry_type: entry.entryType || "article",
        key: key || entry.key || "",
        timeout: 15.0,
      }),
      signal,
    });

    if (!resp.ok) {
      throw new Error(`后端错误 HTTP ${resp.status}`);
    }

    const data = await resp.json();
    return {
      key: data.key || key,
      status: data.status || "unconfirmed",
      classification: data.classification || data.status,
      reasons: data.reasons || data.evidence || [],
      evidence: data.evidence || data.reasons || [],
      score: data.score || 0,
      provider_errors: data.provider_errors || {},
      field_comparison: data.field_comparison || {},
      candidates: (data.candidates || []).map((c) => ({
        source: c.source || "",
        title: c.title || "",
        authors: c.authors || [],
        year: c.year || null,
        venue: c.venue || "",
        url: c.url || "",
        identifier: c.identifier || "",
        score: c.score || 0,
        title_score: c.title_score || 0,
        author_score: c.author_score || 0,
        year_score: c.year_score || null,
        identifier_match: c.identifier_match || false,
        conflicts: c.conflicts || [],
        evidence: c.evidence || [],
      })),
    };
  };
}
