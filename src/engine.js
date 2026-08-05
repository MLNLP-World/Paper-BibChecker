// 检查编排与并发调度。纯前端只有 OpenAlex + Crossref 两个源，因此把 Python
// check_entry 的多阶段策略简化为：先做标识符查询（两源），若已决定则结束；
// 否则做标题发现。分类与字段比对完全复用 checker.js（与 Python 一致）。
//
// 已知偏差：authoritative（会议官网否定）在纯前端恒为 0，故“疑似幻觉”更保守
// ——这是第二层瘦后端要补的能力，此处预留 fallback 钩子。
import { finishCheck, titleSimilarity, entryTitle } from "./checker.js";
import { PROVIDERS } from "./providers.js";

// 复刻 check_entry 里对“可结论”的判断需要的子集：这里用 finishCheck 的产出
// 来决定是否还要继续做标题发现（有可靠候选/标识符命中即停）。
function decisiveFromResult(result) {
  // identifier 命中且标题匹配，或可靠标题发现 → 视为可结论。
  return result.candidates.some(
    (c) =>
      (c.identifier_match && c.title_score >= 0.9) ||
      (c.title_score >= 0.9 && c.author_score >= 0.5)
  );
}

export async function checkEntry(entry, { email, signal, providerNames } = {}) {
  const names = providerNames || Object.keys(PROVIDERS);
  const opts = { email, signal };
  const identifierCandidates = [];
  const discoveryCandidates = [];
  const providerErrors = {};
  const completedProviders = new Set();
  let identifierSuccess = false;
  let noMatchSources = 0;

  const hasDoi = Boolean(String(entry.getField("doi") || ""));
  const hasArxiv = Boolean(String(entry.getField("arxiv_id", "arxiv", "eprint") || ""));
  const title = entryTitle(entry);

  // 阶段 1：标识符查询（仅当有 DOI/arXiv）。OpenAlex/Crossref 都按 DOI 查。
  if (hasDoi || hasArxiv) {
    await Promise.all(
      names.map(async (name) => {
        const prov = PROVIDERS[name];
        if (!prov || !prov.identifier) return;
        try {
          const result = await prov.identifier(entry, opts);
          if (result != null) {
            identifierSuccess = true;
            completedProviders.add(name);
            identifierCandidates.push(...result);
          }
        } catch (error) {
          providerErrors[`${name}:identifier`] = String(error.message || error);
        }
      })
    );
    const interim = buildResult();
    if (decisiveFromResult(interim)) return interim;
  }

  // 阶段 2：标题发现。
  if (title) {
    await Promise.all(
      names.map(async (name) => {
        const prov = PROVIDERS[name];
        if (!prov || !prov.title) return;
        try {
          const titleCandidates = await prov.title(entry, opts);
          discoveryCandidates.push(...titleCandidates);
          const hasClose = titleCandidates.some(
            (c) => titleSimilarity(title, c.title) >= 0.8
          );
          if (prov.academic) noMatchSources += hasClose ? 0 : 1;
          completedProviders.add(name);
        } catch (error) {
          providerErrors[`${name}:title`] = String(error.message || error);
        }
      })
    );
  }

  return buildResult();

  function buildResult() {
    return finishCheck({
      entry,
      identifierCandidates,
      discoveryCandidates,
      providerErrors,
      completed: completedProviders.size,
      noMatchSources,
      authoritativeMisses: 0, // 纯前端无官方源
      identifierSuccess,
    });
  }
}

// 并发池：对多个条目并行检查，限制同时进行数（对应 UI 的“并行数量”）。
// 每完成一条通过 onResult(index, key, result) 回调，实现逐条产出。
export async function checkAll(entries, keys, { concurrency = 4, email, signal, onResult, fallback } = {}) {
  let cursor = 0;
  let completed = 0;
  const total = keys.length;

  async function worker() {
    for (;;) {
      const i = cursor++;
      if (i >= total) return;
      const key = keys[i];
      const entry = entries.get(key);
      let result;
      try {
        result = await checkEntry(entry, { email, signal });
        // 第二层兜底钩子：查不到（unconfirmed）且配置了后端时，交给后端复查。
        if (fallback && result.status === "unconfirmed") {
          try {
            const deep = await fallback(entry, key, { signal });
            if (deep) result = deep;
          } catch (_) {
            /* 兜底失败则保留前端结论 */
          }
        }
      } catch (error) {
        result = {
          key,
          status: "unconfirmed",
          classification: "unconfirmed",
          reasons: ["检查出错：" + String(error.message || error)],
          evidence: [],
          score: 0,
          provider_errors: {},
          field_comparison: {},
          candidates: [],
        };
      }
      completed++;
      if (onResult) onResult(completed, key, result);
    }
  }

  const pool = Array.from({ length: Math.max(1, concurrency) }, () => worker());
  await Promise.all(pool);
}
