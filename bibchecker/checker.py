"""通过学术元数据来源核验参考文献条目。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from .models import BibEntry, Candidate as ModelCandidate, CheckResult as ModelCheckResult
from .providers import Provider, default_providers

VALIDATED = "validated"
NEEDS_REVIEW = "needs_review"
LIKELY_HALLUCINATION = "likely_hallucination"
UNCONFIRMED = "unconfirmed"

FIELD_LABELS = {
    "title": "标题",
    "authors": "作者",
    "year": "年份",
    "venue": "会议或期刊",
    "doi": "DOI",
    "arxiv_id": "arXiv ID",
}

# 为第一版的调用方保留向后兼容的名称。
VERIFIED = VALIDATED
PLAUSIBLE = VALIDATED
CONFLICT = NEEDS_REVIEW


@dataclass
class Candidate(ModelCandidate):
    score: float = 0.0
    title_score: float = 0.0
    author_score: float = 0.0
    year_score: float | None = None
    identifier_match: bool = False
    conflicts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class CheckResult(ModelCheckResult):
    score: float = 0.0
    provider_errors: dict[str, str] = field(default_factory=dict)
    field_comparison: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def classification(self) -> str:
        return self.status

    @property
    def evidence(self) -> list[str]:
        return self.reasons

    @property
    def best_candidate(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data.update(
            classification=self.status,
            score=self.score,
            evidence=self.reasons,
            provider_errors=self.provider_errors,
            field_comparison=self.field_comparison,
        )
        for output, candidate in zip(data["candidates"], self.candidates):
            if isinstance(candidate, Candidate):
                output.update(
                    score=candidate.score,
                    title_score=candidate.title_score,
                    author_score=candidate.author_score,
                    year_score=candidate.year_score,
                    identifier_match=candidate.identifier_match,
                    conflicts=candidate.conflicts,
                    evidence=candidate.evidence,
                )
        return data

    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True)
class _EntryPlan:
    has_url: bool
    has_arxiv: bool
    has_doi: bool
    official: tuple[tuple[int, Provider], ...]

    @property
    def has_identifier(self) -> bool:
        return self.has_arxiv or self.has_doi


def check_entry(
    entry: Any, providers: Iterable[Provider] | Provider | None = None
) -> CheckResult:
    provider_list = _provider_list(providers)
    identifier_candidates: list[ModelCandidate] = []
    discovery_candidates: list[ModelCandidate] = []
    provider_errors: dict[str, str] = {}
    completed = 0
    no_match_sources = 0
    authoritative_misses = 0
    identifier_success = False
    completed_providers: set[int] = set()
    queried_identifiers: set[int] = set()
    queried_titles: set[int] = set()
    applicable: list[tuple[int, Provider]] = []

    for index, provider in enumerate(provider_list):
        name = _provider_name(provider)
        try:
            applies = getattr(provider, "applies", None)
            if applies is not None and not applies(entry):
                continue
        except Exception as error:
            provider_errors[f"{name}:applies"] = str(error)
            continue
        applicable.append((index, provider))
    plan = _entry_plan(entry, applicable)

    def run_identifiers(selected: Sequence[tuple[int, Provider]]) -> int:
        nonlocal identifier_success
        selected = [
            item
            for item in selected
            if item[0] not in queried_identifiers
            and _supports_identifier(item[1])
        ]
        if not selected:
            return 0
        before = len(identifier_candidates)
        for index, provider, result, error in _parallel_provider_calls(
            selected, entry, "identifier"
        ):
            queried_identifiers.add(index)
            name = _provider_name(provider)
            if error is not None:
                provider_errors[f"{name}:identifier"] = str(error)
                continue
            if result is not None:
                identifier_success = True
                completed_providers.add(index)
                identifier_candidates.extend(_provider_result(result)[0])
        return len(identifier_candidates) - before

    def run_titles(selected: Sequence[tuple[int, Provider]]) -> None:
        nonlocal no_match_sources, authoritative_misses
        selected = [
            item for item in selected if item[0] not in queried_titles
        ]
        if not selected:
            return
        for index, provider, result, error in _parallel_provider_calls(
            selected, entry, "title"
        ):
            queried_titles.add(index)
            name = _provider_name(provider)
            if error is not None:
                provider_errors[f"{name}:title"] = str(error)
                continue
            title_candidates = _provider_result(result)[0]
            discovery_candidates.extend(title_candidates)
            has_close_title = any(
                _title_similarity(_entry_title(entry), candidate.title) >= 0.80
                for candidate in title_candidates
            )
            if getattr(provider, "academic_source", True):
                no_match_sources += int(not has_close_title)
                if (
                    getattr(provider, "authoritative", False)
                    and not has_close_title
                ):
                    authoritative_misses += 1
            completed_providers.add(index)

    def finish() -> CheckResult:
        result = _finish_check(
            entry=entry,
            provider_count=len(provider_list),
            identifier_candidates=identifier_candidates,
            discovery_candidates=discovery_candidates,
            provider_errors=provider_errors,
            completed=len(completed_providers),
            no_match_sources=no_match_sources,
            authoritative_misses=authoritative_misses,
            identifier_success=identifier_success,
        )
        # 已得出明确分类时，失败的补充数据源不影响结论，也不应让正常结果
        # 看起来像一次超时运行。只有“无法确认”才保留失败信息。
        if result.status != UNCONFIRMED and (
            result.candidates or authoritative_misses
        ):
            result.provider_errors.clear()
        return result

    def decisive() -> bool:
        candidates = _rank(
            entry,
            identifier_candidates + discovery_candidates,
            prefer_identifier=True,
        )
        return any(
            _identifier_record_matches(entry, candidate)
            or _identifier_identity_supported(candidate)
            or _is_reliable_discovery(candidate)
            for candidate in candidates
        )

    # 1. 显式链接成本最低，也能提供最直接的证据。GitHub 链接使用其 API；
    # 普通论文页面则读取引用元标签。
    link_stage = [
        item
        for item in applicable
        if plan.has_url and _provider_name(item[1]) in {"url", "github"}
    ]
    run_identifiers(link_stage)
    run_titles(link_stage)
    if decisive():
        return finish()

    # 2. 稳定标识符优先于标题搜索。先查询 arXiv；只有 arXiv 没有返回记录时，
    # 才使用 DataCite 作为回退来源。
    if plan.has_arxiv:
        arxiv = _named_providers(applicable, {"arxiv"})
        found = run_identifiers(arxiv)
        if decisive():
            return finish()
        if found == 0:
            run_identifiers(_named_providers(applicable, {"datacite"}))
            if decisive():
                return finish()
        run_identifiers(_named_providers(applicable, {"semanticscholar"}))
        if decisive():
            return finish()

    if plan.has_doi:
        run_identifiers(_named_providers(applicable, {"crossref"}))
        if decisive():
            return finish()
        run_identifiers(_named_providers(applicable, {"openalex"}))
        if decisive():
            return finish()
        run_identifiers(_named_providers(applicable, {"semanticscholar"}))
        if decisive():
            return finish()

    if plan.has_identifier:
        builtins = {
            "url",
            "github",
            "arxiv",
            "datacite",
            "crossref",
            "openalex",
            "semanticscholar",
        }
        run_identifiers(
            [
                item
                for item in applicable
                if _provider_name(item[1]) not in builtins
            ]
        )
        if decisive():
            return finish()

    # 3. 先查询会议/期刊专属的官方来源，再查询通用索引。OpenReview 是
    # COLM 的会议来源，同时也是 ICLR 的回退来源。
    run_titles(plan.official)
    if decisive():
        return finish()
    if authoritative_misses:
        return finish()

    run_titles(_named_providers(applicable, {"openreview"}))
    if decisive():
        return finish()

    # 4. 最后并行查询相互独立的通用索引。arXiv 的标题 API 和 DBLP 的批量
    # 标题搜索容易限流或断开，因此默认流程只用 arXiv 做稳定标识符核验，
    # 标题发现交给 OpenAlex、Crossref 等来源。已有其他索引时，DataCite
    # 的标题搜索会产生重复结果，因此也只把它用作标识符回退。
    general = [
        item
        for item in applicable
        if item[0] not in queried_titles
        and _provider_name(item[1]) not in {"url", "github", "arxiv"}
    ]
    if len(general) > 1:
        general = [
            item for item in general if _provider_name(item[1]) != "datacite"
        ]
    run_titles(general)
    completed = len(completed_providers)

    return finish()


def _finish_check(
    *,
    entry: Any,
    provider_count: int,
    identifier_candidates: Sequence[ModelCandidate],
    discovery_candidates: Sequence[ModelCandidate],
    provider_errors: dict[str, str],
    completed: int,
    no_match_sources: int,
    authoritative_misses: int,
    identifier_success: bool,
) -> CheckResult:

    # 标识符查询和标题发现解决的是不同问题。在标识符结果集中，应优先考虑
    # 标识符命中的记录；但不能仅因某个无关的标题搜索结果带有 BibTeX 标识符，
    # 就把它提升为最佳发现候选。
    ranked_identifiers = _rank(
        entry, identifier_candidates, prefer_identifier=True
    )
    ranked_discovery = _rank(entry, discovery_candidates)
    ranked = _unique_candidates(ranked_identifiers + ranked_discovery)
    best_identifier = _best_identifier(ranked_identifiers)
    best_discovery = _best_discovery(ranked_discovery)
    # 即使某个排名最高的原始发现候选被明确排除为身份匹配，也要保留它用于诊断。
    # 分类只能使用符合条件的候选，但报告应解释为什么没有接受这个看似相近的
    # 候选。
    # 不要把 Bib 条目与无关的“尽力而为”搜索结果进行字段比较。这类结果可作为
    # 原始候选辅助诊断，但如果像对待目标记录一样展示其字段，会让一次检索失败
    # 看起来像参考文献冲突。
    comparison_candidate = (
        best_identifier
        or best_discovery
        or (
            ranked_discovery[0]
            if ranked_discovery and _is_plausible_discovery(ranked_discovery[0])
            else None
        )
    )
    field_comparison = (
        _compare_fields(entry, comparison_candidate)
        if comparison_candidate
        else {}
    )

    status, score, reasons = _classify(
        entry=entry,
        best_identifier=best_identifier,
        best_discovery=best_discovery,
        discovery_candidate_count=sum(
            int(_is_plausible_discovery(item)) for item in ranked_discovery
        ),
        has_discovery_candidates=bool(discovery_candidates),
        no_match_sources=no_match_sources,
        authoritative_misses=authoritative_misses,
        identifier_success=identifier_success,
        completed=completed,
        provider_count=provider_count,
        provider_errors=provider_errors,
        field_comparison=field_comparison,
    )
    return CheckResult(
        key=_entry_key(entry),
        status=status,
        reasons=reasons,
        candidates=ranked[:5],
        score=round(score, 3),
        provider_errors=provider_errors,
        field_comparison=field_comparison,
    )


def _provider_name(provider: Provider) -> str:
    return str(getattr(provider, "name", provider.__class__.__name__))


def _entry_plan(
    entry: Any,
    providers: Sequence[tuple[int, Provider]],
) -> _EntryPlan:
    return _EntryPlan(
        has_url=bool(_entry_url(entry)),
        has_arxiv=bool(_entry_arxiv(entry)),
        has_doi=bool(_entry_doi(entry)),
        official=tuple(
            item
            for item in providers
            if getattr(item[1], "authoritative", False)
        ),
    )


def _supports_identifier(provider: Provider) -> bool:
    return bool(
        getattr(provider, "identifier_lookup", False)
        or "lookup_identifier" in provider.__class__.__dict__
    )


def _named_providers(
    providers: Sequence[tuple[int, Provider]], names: set[str]
) -> list[tuple[int, Provider]]:
    return [
        item for item in providers if _provider_name(item[1]) in names
    ]


def _parallel_provider_calls(
    providers: Sequence[tuple[int, Provider]],
    entry: Any,
    mode: str,
) -> list[tuple[int, Provider, Any, Exception | None]]:
    def call(item: tuple[int, Provider]) -> tuple[int, Provider, Any, Exception | None]:
        index, provider = item
        try:
            if mode == "identifier":
                result = provider.lookup_identifier(entry)
            elif type(provider).search_title is Provider.search_title:
                result = provider.search(entry)
            else:
                result = provider.search_title(entry)
            return index, provider, result, None
        except Exception as error:
            return index, provider, None, error

    if len(providers) <= 1:
        return [call(provider) for provider in providers]
    with ThreadPoolExecutor(max_workers=min(4, len(providers))) as executor:
        return list(executor.map(call, providers))


def check_entries(
    entries: Iterable[Any] | Mapping[str, Any],
    providers: Iterable[Provider] | Provider | Iterable[str] | None = None,
    legacy_providers: Iterable[Provider] | Provider | None = None,
    *,
    workers: int = 1,
) -> list[CheckResult]:
    """检查条目，同时保留原有的三参数兼容接口。"""

    if legacy_providers is not None:
        if not isinstance(entries, Mapping):
            raise TypeError("按 key 选择条目时，必须传入条目映射")
        selected = [entries[key] for key in list(providers or []) if key in entries]
        provider_list = _provider_list(legacy_providers)
    else:
        selected = list(entries.values()) if isinstance(entries, Mapping) else list(entries)
        provider_list = _provider_list(providers)

    if workers <= 1:
        return [check_entry(entry, provider_list) for entry in selected]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(lambda item: check_entry(item, provider_list), selected))


def _classify(
    *,
    entry: Any,
    best_identifier: Candidate | None,
    best_discovery: Candidate | None,
    discovery_candidate_count: int,
    has_discovery_candidates: bool,
    no_match_sources: int,
    authoritative_misses: int,
    identifier_success: bool,
    completed: int,
    provider_count: int,
    provider_errors: Mapping[str, str],
    field_comparison: dict[str, dict[str, Any]],
) -> tuple[str, float, list[str]]:
    if not _entry_title(entry) and not (_entry_doi(entry) or _entry_arxiv(entry)):
        return UNCONFIRMED, 0.0, ["条目缺少标题、DOI 或 arXiv ID"]

    if best_identifier:
        if best_identifier.identifier_match:
            if (
                _is_reliable_discovery(best_discovery)
                and not _same_record(best_identifier, best_discovery)
            ):
                return NEEDS_REVIEW, best_identifier.score, [
                    "标识符指向另一篇论文，但标题和作者检索到了疑似目标论文",
                    *_comparison_reasons(
                        _compare_fields(entry, best_discovery)
                    ),
                    f"标识符记录：{best_identifier.source} - {best_identifier.title}",
                ]
            # 精确标识符加上匹配标题足以确认真实记录。作者、年份和会议/期刊
            # 差异属于字段问题，而不是引用幻觉；标识符是更强的身份信号。
            if _identifier_record_matches(entry, best_identifier):
                return (
                    NEEDS_REVIEW if _has_field_issue(field_comparison) else VALIDATED,
                    best_identifier.score,
                    (
                        ["标识符和标题对应真实论文，但存在字段差异", *_comparison_reasons(field_comparison)]
                        if _has_field_issue(field_comparison)
                        else ["标识符、标题和作者对应真实论文"]
                    ),
                )
            if _identifier_record_mismatch(best_identifier):
                if _is_reliable_discovery(best_discovery):
                    return NEEDS_REVIEW, best_identifier.score, [
                        "标识符指向另一篇论文，但标题搜索找到了疑似目标论文",
                        *_comparison_reasons(field_comparison),
                        f"疑似目标：{best_discovery.source} - {best_discovery.title}",
                    ]
                if _identifier_identity_supported(best_identifier):
                    return NEEDS_REVIEW, best_identifier.score, [
                        "标识符对应真实论文，但标题可能是不同版本的写法",
                        *_comparison_reasons(field_comparison),
                    ]
                if (
                    best_identifier.title_score < 0.75
                    and not _identifier_identity_supported(best_identifier)
                ):
                    return LIKELY_HALLUCINATION, best_identifier.score, [
                        "标识符指向的论文与 Bib 的标题明显不一致，且标题/作者检索未找到目标",
                        *_comparison_reasons(field_comparison),
                    ]
                return UNCONFIRMED, best_identifier.score, [
                    "标识符记录与 Bib 的标题仅部分相似，尚不足以确认是同一篇论文",
                    *_comparison_reasons(field_comparison),
                ]
            if _has_field_issue(field_comparison):
                return NEEDS_REVIEW, best_identifier.score, [
                    "标识符对应真实论文，但存在字段差异",
                    *_comparison_reasons(field_comparison),
                ]
            return VALIDATED, best_identifier.score, [
                "标识符、标题、作者、年份和可用出版信息均一致"
            ]

        if _is_strong_match(best_discovery):
            return NEEDS_REVIEW, best_discovery.score, [
                "标题搜索找到目标论文，但 Bib 中的标识符未对应",
                *_comparison_reasons(
                    _compare_fields(entry, best_discovery)
                ),
            ]
        if identifier_success and not provider_errors:
            return LIKELY_HALLUCINATION, best_identifier.score, [
                "标识符查询返回了记录，但记录与 Bib 不匹配",
                *_comparison_reasons(field_comparison),
            ]
        return UNCONFIRMED, best_identifier.score, [
            "未能把 Bib 标识符与可信论文对应",
            *_comparison_reasons(field_comparison),
        ]

    # 没有 DOI/arXiv 标识符时，仅标题匹配，或标题相似但作者错误，都不足以断言
    # 引用是虚构的。只有可靠的标题和作者匹配才能得到 validated/needs_review。
    if _is_reliable_discovery(best_discovery):
        comparison = _compare_fields(entry, best_discovery)
        if _is_nonacademic_reference(entry):
            if _has_field_issue(comparison):
                return NEEDS_REVIEW, best_discovery.score, [
                    "找到可信网页/博客，但存在字段差异",
                    *_comparison_reasons(comparison),
                ]
            return VALIDATED, best_discovery.score, [
                "标题、作者和年份对应真实网页/博客"
            ]
        if _has_field_issue(comparison):
            return NEEDS_REVIEW, best_discovery.score, [
                "找到可信论文，但存在字段差异",
                *_comparison_reasons(comparison),
            ]
        return VALIDATED, best_discovery.score, [
            "标题、作者、年份和可用出版信息均一致"
        ]

    # 已成功完成的官方会议/期刊和年份索引，比嘈杂的通用搜索候选提供更强的
    # 负面证据。数据源失败不计入该数量，因此超时绝不会变成官方否定。
    if authoritative_misses and not _entry_has_nonacademic_reference(entry):
        return LIKELY_HALLUCINATION, 0.0, [
            f"{authoritative_misses} 个对应会议/期刊的官方数据源"
            "在官方检索范围内未找到可信标题匹配"
        ]

    # 没有找到标题匹配，不足以断言引用是虚构的。只有所有相关来源都正常完成，
    # 且没有任何来源返回足够接近的记录时，才能分类为疑似幻觉。
    no_close_candidate = discovery_candidate_count == 0
    if (
        no_close_candidate
        and _title_is_specific(_entry_title(entry))
        and not _entry_has_nonacademic_reference(entry)
        and (
            no_match_sources >= 3
            or (
                completed >= 2
                and no_match_sources >= 2
                and (
                    not has_discovery_candidates
                    or len(_entry_authors(entry)) >= 3
                )
            )
        )
    ):
        return LIKELY_HALLUCINATION, 0.0, [
            f"{no_match_sources} 个已完成的独立学术数据源均未找到可信标题匹配"
        ]
    return UNCONFIRMED, 0.0, [
        "检索结果不足以确认或否定该引用",
        "没有获得标题和作者同时可靠匹配的记录",
    ]


def _is_identifier_hit_with_matching_title(candidate: Candidate) -> bool:
    return candidate.identifier_match and candidate.title_score >= 0.90


def _is_strong_match(candidate: Candidate | None) -> bool:
    return _is_reliable_discovery(candidate)


def _is_reliable_discovery(candidate: Candidate | None) -> bool:
    """判断标题搜索结果能否可靠地认定为目标论文。"""

    if not candidate:
        return False
    return (
        candidate.title_score >= 0.90
        and candidate.author_score >= 0.50
        and _first_author_match(candidate)
    )


def _is_plausible_discovery(candidate: Candidate | None) -> bool:
    """判断原始标题结果是否足够接近，从而阻止给出负面结论。"""

    if not candidate:
        return False
    if candidate.title_score >= 0.72 and (
        not candidate.authors or candidate.author_score >= 0.25
    ):
        return True
    return candidate.title_score >= 0.60 and _first_author_match(candidate)


def _identifier_record_matches(entry: Any, candidate: Candidate) -> bool:
    """判断精确标识符是否解析到 BibTeX 所描述的记录本身。"""

    # 标识符用于确定记录身份；标题则用于防止 API/数据源返回错误记录。
    # 作者、年份和会议/期刊差异由 field_comparison 记录，并归入信息需核对状态。
    return candidate.identifier_match and _titles_equal(
        _entry_title(entry),
        candidate.title,
        candidate,
    )


def _identifier_record_mismatch(candidate: Candidate) -> bool:
    return candidate.identifier_match and candidate.title_score < 0.90


def _identifier_identity_supported(candidate: Candidate) -> bool:
    """当稳定的作者身份仍然匹配时，接受改名后的记录。"""

    return (
        candidate.identifier_match
        and _first_author_match(candidate)
        and candidate.author_score >= 0.50
    )


def _title_is_specific(title: str) -> bool:
    """保守判断标题是否足够独特，使“未命中”具有参考意义。"""

    tokens = _normalize_text(title).split()
    if len(tokens) < 8:
        return False
    content = [
        token
        for token in tokens
        if len(token) >= 5
        and token
        not in {
            "about",
            "based",
            "from",
            "large",
            "learning",
            "model",
            "models",
            "paper",
            "study",
            "using",
            "with",
        }
    ]
    return len(set(content)) >= 2


def _entry_has_nonacademic_reference(entry: Any) -> bool:
    text = _nonacademic_reference_text(entry)
    return any(
        marker in text
        for marker in (
            "hugging face",
            "repository",
            "dataset",
            "notion.site",
            "github.com",
            "blog",
        )
    )


def _is_nonacademic_reference(entry: Any) -> bool:
    text = _nonacademic_reference_text(entry)
    return any(
        marker in text
        for marker in (
            "notion.site",
            "notion blog",
            "blog",
            "github.com",
            "repository",
            "dataset",
            "hugging face",
        )
    )


def _nonacademic_reference_text(entry: Any) -> str:
    """检查可能携带引用信息的字段，同时避免误把论文标题当成链接。"""

    return " ".join(
        str(_value(entry, name) or "")
        for name in ("url", "howpublished", "note", "repository")
    ).casefold()


def _independent_provider_failures(
    provider_errors: Mapping[str, str],
) -> int:
    return len({name.split(":", 1)[0] for name in provider_errors})


def _first_author_match(candidate: Candidate) -> bool:
    expected = candidate.raw.get("_expected_first_author_text", "")
    actual = candidate.raw.get("_actual_first_author_text", "")
    return _authors_match(expected, actual)


def _rank(
    entry: Any,
    candidates: Sequence[ModelCandidate],
    *,
    prefer_identifier: bool = False,
) -> list[Candidate]:
    ranked = [_score_candidate(entry, item) for item in candidates]
    ranked.sort(
        key=lambda item: (
            item.identifier_match if prefer_identifier else False,
            _is_reliable_discovery(item) if not prefer_identifier else False,
            item.score,
            item.title_score,
            item.author_score,
            (
                int(item.raw.get("arxiv_version") or 0)
                if prefer_identifier and item.source == "arxiv"
                else 0
            ),
        ),
        reverse=True,
    )
    return ranked


def _score_candidate(entry: Any, item: ModelCandidate) -> Candidate:
    title_score = _title_similarity(_entry_title(entry), item.title)
    author_score = _author_similarity(_entry_authors(entry), item.authors)
    year_score = _year_similarity(_entry_year(entry), item.year)
    expected_doi = _entry_doi(entry)
    expected_arxiv = _entry_arxiv(entry)
    candidate_doi = _doi_from_candidate(item)
    candidate_arxiv = _arxiv_from_candidate(item)
    identifier_match = bool(
        (expected_doi and candidate_doi and _normalize_doi(expected_doi) == _normalize_doi(candidate_doi))
        or (
            expected_arxiv
            and candidate_arxiv
            and _normalize_arxiv(expected_arxiv) == _normalize_arxiv(candidate_arxiv)
        )
    )
    conflicts: list[str] = []
    evidence: list[str] = []
    if identifier_match:
        evidence.append(f"{item.source}: 标识符精确匹配")
    if title_score >= 0.88:
        evidence.append(f"{item.source}: 标题相似度 {title_score:.2f}")
    if author_score >= 0.50:
        evidence.append(f"{item.source}: 作者相似度 {author_score:.2f}")
    if year_score == 1.0:
        evidence.append(f"{item.source}: 年份一致")
    score = (
        0.58 * title_score
        + 0.27 * author_score
        + (0.10 * year_score if year_score is not None else 0.05)
        + (0.25 if identifier_match else 0.0)
    )
    if title_score < 0.60:
        conflicts.append(f"{item.source}: 标题明显不一致")
    if _entry_authors(entry) and item.authors and author_score < 0.25:
        conflicts.append(f"{item.source}: 作者明显不一致")
    if _entry_year(entry) and item.year and year_score == 0.0:
        conflicts.append(f"{item.source}: 年份不一致")
    raw = dict(item.raw)
    raw["_expected_first_author"] = _first_surname(_entry_authors(entry))
    raw["_actual_first_author"] = _first_surname(item.authors)
    raw["_expected_first_author_text"] = (
        _entry_authors(entry)[0] if _entry_authors(entry) else ""
    )
    raw["_actual_first_author_text"] = item.authors[0] if item.authors else ""
    return Candidate(
        source=item.source,
        title=item.title,
        authors=list(item.authors),
        year=item.year,
        venue=item.venue,
        url=item.url,
        identifier=item.identifier,
        raw=raw,
        score=round(min(1.0, score), 3),
        title_score=round(title_score, 3),
        author_score=round(author_score, 3),
        year_score=year_score,
        identifier_match=identifier_match,
        conflicts=conflicts,
        evidence=evidence,
    )


def _compare_fields(entry: Any, candidate: Candidate | None) -> dict[str, dict[str, Any]]:
    if not candidate:
        return {}
    expected_authors = _entry_authors(entry)
    expected_venue = _entry_venue(entry)
    title_score = _title_similarity(_entry_title(entry), candidate.title)
    retrieved_venue = candidate.venue
    if candidate.source in {"arxiv", "datacite"} and _venues_equal(
        candidate.venue, "arXiv"
    ):
        retrieved_venue = ""
    titles_equal = _titles_equal(
        _entry_title(entry),
        candidate.title,
        candidate,
    )
    values = {
        "title": (_entry_title(entry), candidate.title, titles_equal),
        "authors": _compare_authors(expected_authors, candidate.authors),
        "year": (_entry_year(entry), candidate.year, _year_similarity(_entry_year(entry), candidate.year) == 1.0),
        "venue": (
            expected_venue,
            retrieved_venue,
            _venues_equal(expected_venue, retrieved_venue),
        ),
        "doi": (_entry_doi(entry), _doi_from_candidate(candidate), _identifier_equal(_entry_doi(entry), _doi_from_candidate(candidate), "doi")),
        "arxiv_id": (_entry_arxiv(entry), _arxiv_from_candidate(candidate), _identifier_equal(_entry_arxiv(entry), _arxiv_from_candidate(candidate), "arxiv")),
    }
    result: dict[str, dict[str, Any]] = {}
    for name, value in values.items():
        if name == "authors":
            result[name] = value
            continue
        expected, actual, equal = value
        if expected in ("", None, []) or actual in ("", None, []):
            status = "not_available"
        else:
            if name == "title" and not equal and title_score >= 0.72:
                status = "compatible"
            else:
                status = "match" if equal else "mismatch"
        result[name] = {"bibtex": expected, "retrieved": actual, "status": status}
    return result


def _titles_equal(
    expected: str,
    actual: str,
    candidate: Candidate,
) -> bool:
    if _normalize_text(expected) == _normalize_text(actual):
        return True
    if candidate.title_score >= 0.90:
        return True
    return bool(
        candidate.identifier_match
        and candidate.author_score >= 0.50
        and _first_author_match(candidate)
        and _title_version_key(expected)
        and _title_version_key(expected) == _title_version_key(actual)
    )


def _title_version_key(title: str) -> str:
    """返回多个标题修订版本共有、且具有辨识度的方法名前缀。"""

    prefix, separator, _ = title.partition(":")
    key = _normalize_text(prefix)
    if not separator or len(key.replace(" ", "")) < 5:
        return ""
    if key in {
        "analysis",
        "introduction",
        "overview",
        "study",
        "survey",
        "towards",
    }:
        return ""
    return key


def _comparison_reasons(comparison: Mapping[str, Mapping[str, Any]]) -> list[str]:
    output = []
    for field, item in comparison.items():
        if item.get("status") in {
            "mismatch",
            "minor_difference",
            "major_mismatch",
        } or (field == "title" and item.get("status") == "compatible"):
            if field == "authors":
                output.append(_author_diff_summary(item))
                continue
            label = FIELD_LABELS.get(field, field)
            output.append(
                f"{label}：Bib={item.get('bibtex')!r}；检索={item.get('retrieved')!r}"
            )
    return output or ["可比字段没有发现明确差异"]


def _has_mismatch(comparison: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(item.get("status") == "mismatch" for item in comparison.values())


def _has_field_issue(comparison: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(
        item.get("status") in {"mismatch", "minor_difference", "major_mismatch"}
        or (field == "title" and item.get("status") == "compatible")
        for field, item in comparison.items()
    )


def _author_major_mismatch(
    comparison: Mapping[str, Mapping[str, Any]]
) -> bool:
    return comparison.get("authors", {}).get("status") == "major_mismatch"


def _severe_mismatch(candidate: Candidate) -> bool:
    return candidate.title_score < 0.75 and not _first_author_match(candidate)


def _best_identifier(candidates: Sequence[Candidate]) -> Candidate | None:
    return next((item for item in candidates if item.identifier_match), None) or (
        candidates[0] if candidates else None
    )


def _best_discovery(candidates: Sequence[Candidate]) -> Candidate | None:
    # 不要让一个仅仅相似的最高排名结果左右分类。可靠候选的加权分数可能低于
    # 噪声结果，因此应先从符合条件的候选子集中选择。
    reliable = [item for item in candidates if _is_reliable_discovery(item)]
    return reliable[0] if reliable else None


def _same_record(left: Candidate | None, right: Candidate | None) -> bool:
    if not left or not right:
        return False
    if left.identifier and right.identifier:
        left_doi = _doi_from_candidate(left)
        right_doi = _doi_from_candidate(right)
        if left_doi and right_doi:
            return _normalize_doi(left_doi) == _normalize_doi(right_doi)
        left_arxiv = _arxiv_from_candidate(left)
        right_arxiv = _arxiv_from_candidate(right)
        if left_arxiv and right_arxiv:
            return _normalize_arxiv(left_arxiv) == _normalize_arxiv(right_arxiv)
    return (
        left.source == right.source
        and _title_similarity(left.title, right.title) >= 0.98
        and _author_similarity(left.authors, right.authors) >= 0.95
    )


def _unique_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    result: list[Candidate] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        identity = (
            candidate.source,
            _normalize_text(candidate.title),
            candidate.identifier,
        )
        if identity not in seen:
            seen.add(identity)
            result.append(candidate)
    return result


def _provider_list(providers: Iterable[Provider] | Provider | None) -> list[Provider]:
    if providers is None:
        return list(default_providers())
    if any(
        hasattr(providers, method)
        for method in ("search", "search_title", "lookup_identifier")
    ):
        return [providers]  # type: ignore[list-item]
    return list(providers)


def _provider_result(result: Any) -> tuple[list[ModelCandidate], bool]:
    if result is None:
        return [], False
    if hasattr(result, "candidates"):
        return list(result.candidates), bool(getattr(result, "definitive", False))
    return list(result), bool(getattr(result, "definitive", False))


def _author_similarity(expected: Sequence[str], actual: Sequence[str]) -> float:
    left = [author for author in expected if not _is_truncation_marker(author)]
    right = [author for author in actual if not _is_truncation_marker(author)]
    if not left or not right:
        return 0.0
    pairs = _match_authors(left, right)
    if any(_is_truncation_marker(item) for item in expected) and len(pairs) == len(left):
        return 1.0
    matches = len(pairs)
    precision, recall = matches / len(right), matches / len(left)
    return 2 * precision * recall / (precision + recall) if matches else 0.0


def _title_similarity(left: str, right: str) -> float:
    left, right = _normalize_text(left), _normalize_text(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = overlap / union if union else 0.0
    containment = overlap / min(len(left_tokens), len(right_tokens))
    return max(SequenceMatcher(None, left, right).ratio(), 0.55 * jaccard + 0.45 * containment)


def _year_similarity(expected: int | None, actual: int | None) -> float | None:
    if expected is None or actual is None:
        return None
    difference = abs(expected - actual)
    return 1.0 if difference == 0 else 0.7 if difference == 1 else 0.0


def _entry_key(entry: Any) -> str:
    return str(_value(entry, "key", "citation_key", "id") or "")


def _entry_title(entry: Any) -> str:
    return str(_value(entry, "title") or "")


def _entry_authors(entry: Any) -> list[str]:
    authors = _value(entry, "authors", "author") or []
    if isinstance(authors, str):
        return [part.strip() for part in re.split(r"\s+and\s+", authors, flags=re.I) if part.strip()]
    return [str(item) for item in authors]


def _entry_year(entry: Any) -> int | None:
    match = re.search(r"\d{4}", str(_value(entry, "year") or ""))
    return int(match.group()) if match else None


def _entry_doi(entry: Any) -> str:
    return str(_value(entry, "doi") or "")


def _entry_arxiv(entry: Any) -> str:
    return str(_value(entry, "arxiv_id", "arxiv", "eprint") or "")


def _entry_url(entry: Any) -> str:
    fields = getattr(entry, "fields", None)
    values = (
        fields.values()
        if isinstance(fields, Mapping)
        else (
            _value(entry, "url"),
            _value(entry, "howpublished"),
            _value(entry, "note"),
        )
    )
    return next(
        (
            match.group()
            for value in values
            if (
                match := re.search(
                    r"https?://[^\s{}<>]+",
                    str(value or ""),
                    flags=re.IGNORECASE,
                )
            )
        ),
        "",
    )


def _entry_venue(entry: Any) -> str:
    return str(_value(entry, "journal", "booktitle", "venue") or "")


def _entry_field_values(entry: Any) -> list[str]:
    fields = getattr(entry, "fields", None)
    if isinstance(fields, Mapping):
        return [str(value) for value in fields.values()]
    return []


def _has_github_reference(entry: Any) -> bool:
    return any("github" in value.casefold() for value in _entry_field_values(entry))


def _value(entry: Any, *names: str) -> Any:
    if isinstance(entry, Mapping):
        lower = {str(key).lower(): value for key, value in entry.items()}
        for name in names:
            if name.lower() in lower:
                return lower[name.lower()]
        return None
    fields = getattr(entry, "fields", None)
    if isinstance(fields, Mapping):
        lower = {str(key).lower(): value for key, value in fields.items()}
        for name in names:
            if name.lower() in lower:
                return lower[name.lower()]
    for name in names:
        if hasattr(entry, name):
            return getattr(entry, name)
    return None


def _doi_from_candidate(item: ModelCandidate) -> str:
    values = [item.identifier or "", item.url or "", str(item.raw.get("doi", "")), str(item.raw.get("DOI", ""))]
    for value in values:
        match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", value, flags=re.I)
        if match:
            return match.group().rstrip(".,;)")
    return ""


def _arxiv_from_candidate(item: ModelCandidate) -> str:
    values = [item.identifier or "", item.url or "", str(item.raw.get("arxiv_id", "")), str(item.raw.get("eprint", ""))]
    for value in values:
        match = re.search(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?", value)
        if match:
            return match.group(1)
    return ""


def _normalize_doi(value: str) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I).casefold().rstrip(".,;)")


def _normalize_arxiv(value: str) -> str:
    value = re.sub(r"^.*?arxiv(?:\.org/(?:abs|pdf)/|:)", "", value.strip(), flags=re.I)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    return re.sub(r"v\d+$", "", value, flags=re.I).casefold()


def _identifier_equal(expected: str, actual: str, kind: str) -> bool:
    if not expected or not actual:
        return False
    return (
        _normalize_doi(expected) == _normalize_doi(actual)
        if kind == "doi"
        else _normalize_arxiv(expected) == _normalize_arxiv(actual)
    )


def _text_equal(left: str, right: str) -> bool:
    return bool(left and right and _normalize_text(left) == _normalize_text(right))


def _authors_equal(left: Sequence[str], right: Sequence[str]) -> bool:
    return _compare_authors(left, right)["status"] == "match"


def _compare_authors(
    expected: Sequence[str], actual: Sequence[str]
) -> dict[str, Any]:
    expected_names = [
        author for author in expected if not _is_truncation_marker(author)
    ]
    actual_names = [
        author for author in actual if not _is_truncation_marker(author)
    ]
    if not expected_names or not actual_names:
        return {
            "bibtex": list(expected),
            "retrieved": list(actual),
            "status": "not_available",
            "added": [],
            "removed": [],
            "reordered": [],
        }

    truncated = any(_is_truncation_marker(author) for author in expected)
    pairs = _match_authors(expected_names, actual_names)
    expected_matched = {expected_index for expected_index, _ in pairs}
    actual_matched = {actual_index for _, actual_index in pairs}
    added = [
        f"{_display_author(actual_names[index])}（检索第{index + 1}位）"
        for index in range(len(actual_names))
        if index not in actual_matched
    ]
    removed = [
        f"{_display_author(expected_names[index])}（Bib第{index + 1}位）"
        for index in range(len(expected_names))
        if index not in expected_matched
    ]
    reordered = _reordered_authors(pairs, actual_names)
    overlap = len(pairs) / max(len(expected_names), len(actual_names))

    if truncated:
        # ``others`` 表示 Bib 作者列表只是部分记录。已匹配的具名作者可以证明
        # 两边一致，但无法从有意截断的列表中可靠推断后续作者的增删情况。
        status = "match" if pairs else "major_mismatch"
        added = []
        removed = []
        reordered = []
    elif not added and not removed and not reordered:
        status = "match"
    elif overlap >= 0.6:
        status = "minor_difference"
    else:
        status = "major_mismatch"
    return {
        "bibtex": list(expected),
        "retrieved": list(actual),
        "status": status,
        "added": added,
        "removed": removed,
        "reordered": reordered,
        "overlap": round(overlap, 3),
    }


def _author_diff_summary(item: Mapping[str, Any]) -> str:
    parts = ["authors:"]
    added = item.get("added") or []
    removed = item.get("removed") or []
    reordered = item.get("reordered") or []
    if added:
        parts.append("新增 " + "；".join(added))
    if removed:
        parts.append("删除 " + "；".join(removed))
    if reordered:
        parts.append("顺序调整 " + "；".join(reordered))
    if len(parts) == 1:
        parts.append("作者列表存在差异")
    return " ".join(parts)


def _author_identity(author: str) -> tuple[str, tuple[str, ...]]:
    value = _normalize_text(author)
    if not value:
        return "", ()
    if "," in author:
        family, given = author.split(",", 1)
        return _normalize_text(family), tuple(_normalize_text(given).split())
    tokens = value.split()
    return tokens[-1], tuple(tokens[:-1])


def _given_names_match(
    left: Sequence[str],
    right: Sequence[str],
) -> bool:
    """Match full given names while allowing explicit initials."""

    if not left or not right:
        return True
    common_length = min(len(left), len(right))
    for left_token, right_token in zip(left[:common_length], right[:common_length]):
        if left_token == right_token:
            continue
        if (
            len(left_token) == 1
            and right_token.startswith(left_token)
        ) or (
            len(right_token) == 1
            and left_token.startswith(right_token)
        ):
            continue
        return False
    extra_tokens = (
        left[common_length:] if len(left) > common_length else right[common_length:]
    )
    return all(len(token) == 1 for token in extra_tokens)


def _authors_match(left: str, right: str) -> bool:
    left_family, left_given = _author_identity(left)
    right_family, right_given = _author_identity(right)
    if not left_family or not right_family:
        return False
    family_match = (
        left_family == right_family
        or left_family.split()[-1] == right_family.split()[-1]
    )
    if not family_match:
        return False
    if not left_given or not right_given:
        return True
    return _given_names_match(left_given, right_given)


def _match_authors(
    expected: Sequence[str], actual: Sequence[str]
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    used_actual: set[int] = set()
    for expected_index, expected_author in enumerate(expected):
        for actual_index, actual_author in enumerate(actual):
            if actual_index in used_actual:
                continue
            if _authors_match(expected_author, actual_author):
                pairs.append((expected_index, actual_index))
                used_actual.add(actual_index)
                break
    return pairs


def _reordered_authors(
    pairs: Sequence[tuple[int, int]], actual: Sequence[str]
) -> list[str]:
    if len(pairs) < 2:
        return []
    ordered_pairs = sorted(pairs)
    actual_positions = [actual_index for _, actual_index in ordered_pairs]
    if actual_positions == sorted(actual_positions):
        return []
    rank_by_actual = {
        actual_index: rank
        for rank, actual_index in enumerate(sorted(actual_positions))
    }
    return [
        f"{_display_author(actual[actual_index])}（Bib第{expected_index + 1}位→检索第{actual_index + 1}位）"
        for expected_rank, (expected_index, actual_index) in enumerate(ordered_pairs)
        if rank_by_actual[actual_index] != expected_rank
    ]


def _is_truncation_marker(author: str) -> bool:
    return _normalize_text(author).replace(" ", "") in {
        "others",
        "etal",
        "andothers",
    }


def _display_author(author: str) -> str:
    value = _decode_latex(author)
    value = re.sub(r"[{}]", "", value)
    value = " ".join(value.split()).strip()
    if "," in value:
        family, given = (part.strip() for part in value.split(",", 1))
        return " ".join(part for part in (given, family) if part)
    return value


def _venues_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    canonical_left = _canonical_venue(normalized_left)
    canonical_right = _canonical_venue(normalized_right)
    return (
        canonical_left == canonical_right
        if canonical_left and canonical_right
        else normalized_left == normalized_right
    )


def _canonical_venue(value: str) -> str:
    aliases = (
        ("tacl", ("tacl", "transactions of the association for computational linguistics")),
        ("jmlr", ("jmlr", "journal of machine learning research")),
        ("neurips", ("neurips", "nips", "neural information processing systems")),
        ("iclr", ("iclr", "international conference on learning representations")),
        ("icml", ("icml", "international conference on machine learning")),
        ("cvpr", ("cvpr", "computer vision and pattern recognition")),
        ("iccv", ("iccv", "international conference on computer vision")),
        ("eccv", ("eccv", "european conference on computer vision")),
        ("emnlp", ("emnlp", "empirical methods in natural language processing")),
        ("acl", ("acl", "annual meeting of the association for computational linguistics")),
        ("colm", ("colm", "conference on language modeling")),
        ("arxiv", ("arxiv", "corr")),
    )
    for canonical, markers in aliases:
        if any(re.search(rf"\b{re.escape(marker)}\b", value) for marker in markers):
            return canonical
    return ""


def _normalize_text(value: str) -> str:
    value = _decode_latex(value)
    value = re.sub(r"\\[A-Za-z]+\s*", "", value).replace("{", "").replace("}", "")
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _decode_latex(value: str) -> str:
    accents = {
        "'": "\u0301",
        "`": "\u0300",
        "^": "\u0302",
        '"': "\u0308",
        "~": "\u0303",
        "=": "\u0304",
        ".": "\u0307",
        "u": "\u0306",
        "v": "\u030c",
        "H": "\u030b",
        "c": "\u0327",
        "k": "\u0328",
        "b": "\u0331",
        "d": "\u0323",
        "r": "\u030a",
    }
    pattern = re.compile(
        r"\{?\\(?P<accent>['`^\"~=\.uvHckbdr])"
        r"(?:\{(?P<braced>[A-Za-z])\}|(?P<plain>[A-Za-z]))\}?"
    )

    def replace(match: re.Match[str]) -> str:
        letter = match.group("braced") or match.group("plain") or ""
        return unicodedata.normalize(
            "NFC", letter + accents[match.group("accent")]
        )

    value = pattern.sub(replace, value)
    special = {
        r"\ss": "ß",
        r"\o": "ø",
        r"\O": "Ø",
        r"\l": "ł",
        r"\L": "Ł",
        r"\ae": "æ",
        r"\AE": "Æ",
        r"\oe": "œ",
        r"\OE": "Œ",
    }
    for command, replacement in special.items():
        value = value.replace(command, replacement)
    return value


def _surname(author: str) -> str:
    normalized = _normalize_text(author)
    if not normalized:
        return ""
    if "," in author:
        return _normalize_text(author.split(",", 1)[0]).split()[-1]
    return normalized.split()[-1]


def _first_surname(authors: Sequence[str]) -> str:
    return _surname(authors[0]) if authors else ""


__all__ = [
    "Candidate",
    "CheckResult",
    "CONFLICT",
    "LIKELY_HALLUCINATION",
    "NEEDS_REVIEW",
    "PLAUSIBLE",
    "UNCONFIRMED",
    "VALIDATED",
    "VERIFIED",
    "check_entries",
    "check_entry",
]
