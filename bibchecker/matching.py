"""保守的候选记录与作者匹配基础逻辑。

本模块刻意与检查器的编排逻辑分离，可供检查器对候选记录评分，同时避免把缺失
元数据误当作矛盾证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import BibEntry, Candidate


_ARXIV_RE = re.compile(
    r"(?<!\d)(?P<identifier>"
    r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})"
    r")(?:v\d+)?",
    re.IGNORECASE,
)
_DOI_RE = re.compile(
    r"10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    re.IGNORECASE,
)
_TRUNCATION_RE = re.compile(r"\bet\s*\.?\s*al\.?\b", re.IGNORECASE)
_TRUNCATION_MARKERS = {"others", "etal", "andothers", "etalii", "etalios"}


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    """包含各字段证据和评分的候选记录。"""

    candidate: Candidate
    score: float
    title_score: float
    author_score: float | None
    year_score: float | None
    identifier_match: bool
    identifier_conflict: bool
    confidence: str
    author_comparison: dict[str, Any]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateGroup:
    """从多个数据源汇总得到的同一逻辑记录证据。"""

    representative: CandidateAssessment
    observations: tuple[CandidateAssessment, ...]
    sources: tuple[str, ...]
    score: float


def compare_authors(
    expected: Sequence[str] | str,
    actual: Sequence[str] | str,
    *,
    expected_truncated: bool | None = None,
    actual_truncated: bool | None = None,
    actual_total: int | None = None,
) -> dict[str, Any]:
    """比较作者列表，并区分“未知”和“矛盾”。

    ``others``/``et al.`` 以及 ``actual_total`` 等数据源元数据表示作者列表
    不完整。超出部分列表范围且未匹配的姓名记为 ``unobserved``，而不是
    ``removed``；被截断的 BibTeX 列表中省略的姓名也不会记为 ``added``。
    这样可避免把数据源的作者数量限制误判为论文不匹配。
    """

    expected_names = _author_list(expected)
    actual_names = _author_list(actual)
    expected_marker = any(_is_truncation_marker(name) for name in expected_names)
    actual_marker = any(_is_truncation_marker(name) for name in actual_names)
    expected_names = [
        name for name in expected_names if not _is_truncation_marker(name)
    ]
    actual_names = [
        name for name in actual_names if not _is_truncation_marker(name)
    ]
    expected_truncated = (
        expected_marker if expected_truncated is None else expected_truncated
    )
    actual_truncated = actual_marker if actual_truncated is None else actual_truncated
    if actual_total is not None and actual_total > len(actual_names):
        actual_truncated = True

    if not expected_names or not actual_names:
        return {
            "bibtex": list(expected),
            "retrieved": list(actual),
            "status": "not_available",
            "confidence": "unknown",
            "added": [],
            "removed": [],
            "unobserved": [],
            "observed_extra": [],
            "reordered": [],
            "matched": [],
            "overlap": None,
            "expected_truncated": bool(expected_truncated),
            "retrieved_truncated": bool(actual_truncated),
        }

    pairs = _match_authors(expected_names, actual_names)
    expected_matched = {left for left, _ in pairs}
    actual_matched = {right for _, right in pairs}
    unmatched_expected = [
        (index, name)
        for index, name in enumerate(expected_names)
        if index not in expected_matched
    ]
    unmatched_actual = [
        (index, name)
        for index, name in enumerate(actual_names)
        if index not in actual_matched
    ]

    removed = (
        []
        if actual_truncated or expected_truncated
        else [
            f"{_display_author(name)}（Bib第{index + 1}位）"
            for index, name in unmatched_expected
        ]
    )
    added = (
        []
        if expected_truncated
        else [
            f"{_display_author(name)}（检索第{index + 1}位）"
            for index, name in unmatched_actual
        ]
    )
    unobserved = (
        [
            f"{_display_author(name)}（Bib第{index + 1}位）"
            for index, name in unmatched_expected
        ]
        if actual_truncated
        else []
    )
    observed_extra = (
        [
            f"{_display_author(name)}（检索第{index + 1}位）"
            for index, name in unmatched_actual
        ]
        if expected_truncated
        else []
    )
    reordered = _reordered_authors(pairs, actual_names)
    matched = [
        {
            "bib_index": left + 1,
            "retrieved_index": right + 1,
            "bibtex": expected_names[left],
            "retrieved": actual_names[right],
        }
        for left, right in pairs
    ]
    first_match = any(left == 0 and right == 0 for left, right in pairs)
    overlap = len(pairs) / max(len(expected_names), len(actual_names))
    known_side = (
        min(len(expected_names), len(actual_names))
        if expected_truncated or actual_truncated
        else max(len(expected_names), len(actual_names))
    )
    known_coverage = len(pairs) / known_side if known_side else 0.0

    if not pairs:
        status = "major_mismatch"
        confidence = "contradictory"
    elif expected_truncated or actual_truncated:
        # 部分记录只能证明现有作者一致，不能证明完整作者名单一致。
        status = "partial_match"
        confidence = "partial"
        if not first_match and overlap < 0.5:
            status = "major_mismatch"
            confidence = "contradictory"
    elif not added and not removed and not reordered:
        status = "match"
        confidence = "complete"
    elif not added and not removed and reordered:
        status = "minor_difference"
        confidence = "complete"
    elif overlap >= 0.6 and (first_match or len(expected_names) == 1):
        status = "minor_difference"
        confidence = "complete"
    else:
        status = "major_mismatch"
        confidence = "contradictory"

    return {
        "bibtex": list(expected),
        "retrieved": list(actual),
        "status": status,
        "confidence": confidence,
        "added": added,
        "removed": removed,
        "unobserved": unobserved,
        "observed_extra": observed_extra,
        "reordered": reordered,
        "matched": matched,
        "overlap": round(overlap, 3),
        "known_coverage": round(known_coverage, 3),
        "first_match": first_match,
        "expected_truncated": bool(expected_truncated),
        "retrieved_truncated": bool(actual_truncated),
    }


def assess_candidate(entry: Any, candidate: Candidate) -> CandidateAssessment:
    """评估一条候选记录，并将缺失元数据视为未知。"""

    expected_title = _entry_title(entry)
    expected_authors = _entry_authors(entry)
    expected_year = _entry_year(entry)
    expected_doi = _entry_doi(entry)
    expected_arxiv = _entry_arxiv(entry)

    title_score = _title_similarity(expected_title, candidate.title)
    actual_authors = _author_list(candidate.authors)
    author_comparison = compare_authors(
        expected_authors,
        actual_authors,
        actual_truncated=_candidate_authors_truncated(candidate),
        actual_total=_candidate_author_total(candidate),
    )
    author_score = _author_score(author_comparison)
    year_score = _year_similarity(expected_year, candidate.year)

    candidate_doi = _candidate_doi(candidate)
    candidate_arxiv = _candidate_arxiv(candidate)
    identifier_match = bool(
        expected_doi
        and candidate_doi
        and _normalize_doi(expected_doi) == _normalize_doi(candidate_doi)
    ) or bool(
        expected_arxiv
        and candidate_arxiv
        and _normalize_arxiv(expected_arxiv) == _normalize_arxiv(candidate_arxiv)
    )
    identifier_conflict = identifier_match and (
        title_score < 0.55
        or author_comparison.get("status") == "major_mismatch"
    )

    available_weight = 0.0
    weighted_score = 0.0
    if expected_title and candidate.title:
        available_weight += 0.65
        weighted_score += 0.65 * title_score
    if author_score is not None:
        available_weight += 0.25
        weighted_score += 0.25 * author_score
    if year_score is not None:
        available_weight += 0.10
        weighted_score += 0.10 * year_score
    evidence_score = (
        weighted_score / available_weight if available_weight else 0.0
    )
    if identifier_match:
        score = 0.72 + 0.20 * title_score + 0.08 * (
            author_score if author_score is not None else 0.5
        )
    else:
        score = evidence_score
        if author_score is None and title_score >= 0.95:
            # 仅标题命中的记录可以提供参考，但不能只因缺失字段没有扣分，
            # 就排在字段完整且得到交叉印证的候选之前。
            score = min(score, 0.86)
    if identifier_conflict:
        score *= 0.35

    confidence = _candidate_confidence(
        identifier_match=identifier_match,
        identifier_conflict=identifier_conflict,
        title_score=title_score,
        author_comparison=author_comparison,
        author_score=author_score,
        year_score=year_score,
    )
    reasons = _candidate_reasons(
        candidate,
        title_score=title_score,
        author_score=author_score,
        year_score=year_score,
        identifier_match=identifier_match,
        identifier_conflict=identifier_conflict,
        author_comparison=author_comparison,
    )
    return CandidateAssessment(
        candidate=candidate,
        score=round(max(0.0, min(1.0, score)), 3),
        title_score=round(title_score, 3),
        author_score=None if author_score is None else round(author_score, 3),
        year_score=year_score,
        identifier_match=identifier_match,
        identifier_conflict=identifier_conflict,
        confidence=confidence,
        author_comparison=author_comparison,
        reasons=tuple(reasons),
    )


def rank_candidates(
    entry: Any,
    candidates: Iterable[Candidate],
) -> list[CandidateAssessment]:
    """对候选记录排序，优先采用可靠的身份信息。"""

    assessments = [assess_candidate(entry, candidate) for candidate in candidates]
    return sorted(
        assessments,
        key=lambda item: (
            not item.identifier_conflict,
            item.identifier_match,
            item.confidence in {"identifier", "strong"},
            item.score,
            item.title_score,
            item.author_score if item.author_score is not None else -1.0,
        ),
        reverse=True,
    )


def consolidate_candidates(
    entry: Any,
    candidates: Iterable[Candidate],
) -> list[CandidateGroup]:
    """合并由元数据不完整的数据源提供、但彼此印证的记录。

    DOI/arXiv 精确匹配的记录始终合并。没有标识符时，只有标题几乎一致、年份
    兼容，且作者重合或其中一个来源没有作者元数据，才会合并记录。
    """

    assessments = rank_candidates(entry, candidates)
    groups: list[list[CandidateAssessment]] = []
    for assessment in assessments:
        for group in groups:
            if _same_logical_record(group, assessment):
                group.append(assessment)
                break
        else:
            groups.append([assessment])

    result: list[CandidateGroup] = []
    for group in groups:
        observations = tuple(group)
        representative = max(
            observations,
            key=lambda item: (
                _metadata_richness(item),
                item.score,
                item.title_score,
            ),
        )
        sources = tuple(
            dict.fromkeys(
                str(item.candidate.source)
                for item in observations
                if item.candidate.source
            )
        )
        corroboration_bonus = min(0.08, 0.03 * max(0, len(sources) - 1))
        result.append(
            CandidateGroup(
                representative=representative,
                observations=observations,
                sources=sources,
                score=round(min(1.0, representative.score + corroboration_bonus), 3),
            )
        )
    return sorted(result, key=lambda item: item.score, reverse=True)


def _same_logical_record(
    group: Sequence[CandidateAssessment],
    assessment: CandidateAssessment,
) -> bool:
    current = assessment.candidate
    for existing in group:
        other = existing.candidate
        current_doi = _candidate_doi(current)
        other_doi = _candidate_doi(other)
        if current_doi and other_doi:
            if _normalize_doi(current_doi) == _normalize_doi(other_doi):
                return True
            continue
        current_arxiv = _candidate_arxiv(current)
        other_arxiv = _candidate_arxiv(other)
        if current_arxiv and other_arxiv:
            if _normalize_arxiv(current_arxiv) == _normalize_arxiv(other_arxiv):
                return True
            continue

        title_score = _title_similarity(current.title, other.title)
        if title_score < 0.94 or not _years_compatible(current.year, other.year):
            continue
        left_authors = _author_list(current.authors)
        right_authors = _author_list(other.authors)
        if not left_authors or not right_authors:
            return True
        comparison = compare_authors(left_authors, right_authors)
        if comparison["status"] != "major_mismatch":
            return True
    return False


def _metadata_richness(assessment: CandidateAssessment) -> int:
    candidate = assessment.candidate
    return sum(
        bool(value)
        for value in (
            candidate.title,
            candidate.authors,
            candidate.year,
            _candidate_doi(candidate),
            _candidate_arxiv(candidate),
            candidate.venue,
        )
    )


def _candidate_confidence(
    *,
    identifier_match: bool,
    identifier_conflict: bool,
    title_score: float,
    author_comparison: Mapping[str, Any],
    author_score: float | None,
    year_score: float | None,
) -> str:
    if identifier_conflict:
        return "identifier_conflict"
    if identifier_match:
        return "identifier"
    author_status = author_comparison.get("status")
    if (
        title_score >= 0.92
        and author_status in {"match", "partial_match", "not_available"}
        and (year_score in {None, 0.7, 1.0})
    ):
        if author_status == "not_available":
            return "title_only"
        return "strong"
    if title_score >= 0.82 and author_status != "major_mismatch":
        return "probable"
    return "weak"


def _candidate_reasons(
    candidate: Candidate,
    *,
    title_score: float,
    author_score: float | None,
    year_score: float | None,
    identifier_match: bool,
    identifier_conflict: bool,
    author_comparison: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if identifier_match:
        reasons.append(f"{candidate.source}: 标识符匹配（版本号忽略）")
    if identifier_conflict:
        reasons.append(f"{candidate.source}: 标识符记录与标题/作者冲突")
    if title_score >= 0.8:
        reasons.append(f"{candidate.source}: 标题相似度 {title_score:.2f}")
    if author_score is None:
        reasons.append(f"{candidate.source}: 作者字段不可用，未作为反证")
    elif author_comparison.get("status") in {"partial_match", "minor_difference"}:
        reasons.append(f"{candidate.source}: 作者列表部分一致或存在轻微差异")
    elif author_comparison.get("status") == "major_mismatch":
        reasons.append(f"{candidate.source}: 作者列表明显不一致")
    if year_score == 1.0:
        reasons.append(f"{candidate.source}: 年份一致")
    elif year_score == 0.7:
        reasons.append(f"{candidate.source}: 年份相差一年，可能是预印本/正式发表年份")
    return reasons


def _author_score(comparison: Mapping[str, Any]) -> float | None:
    if comparison.get("status") == "not_available":
        return None
    pairs = len(comparison.get("matched") or [])
    expected = [
        name
        for name in comparison.get("bibtex", [])
        if not _is_truncation_marker(name)
    ]
    actual = [
        name
        for name in comparison.get("retrieved", [])
        if not _is_truncation_marker(name)
    ]
    if not expected or not actual:
        return None
    if comparison.get("expected_truncated") or comparison.get("retrieved_truncated"):
        return pairs / min(len(expected), len(actual))
    return 2 * pairs / (len(expected) + len(actual))


def _candidate_authors_truncated(candidate: Candidate) -> bool:
    raw = candidate.raw or {}
    for key in (
        "authors_truncated",
        "author_truncated",
        "authors_complete",
        "author_complete",
    ):
        if key in raw:
            value = raw[key]
            if key.endswith("complete"):
                return not bool(value)
            return bool(value)
    return any(_is_truncation_marker(author) for author in candidate.authors)


def _candidate_author_total(candidate: Candidate) -> int | None:
    raw = candidate.raw or {}
    for key in ("total_authors", "author_count", "authors_count"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _author_list(value: Sequence[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [
            item.strip()
            for item in re.split(r"\s+and\s+", value, flags=re.IGNORECASE)
            if item.strip()
        ]
    return [str(item).strip() for item in value if str(item).strip()]


def _entry_title(entry: Any) -> str:
    return str(_value(entry, "title") or "")


def _entry_authors(entry: Any) -> list[str]:
    value = _value(entry, "authors")
    if value is None:
        value = _value(entry, "author")
    return _author_list(value)


def _entry_year(entry: Any) -> int | None:
    value = _value(entry, "year")
    match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _entry_doi(entry: Any) -> str:
    return str(_value(entry, "doi") or "")


def _entry_arxiv(entry: Any) -> str:
    return str(_value(entry, "arxiv_id", "arxiv", "eprint") or "")


def _value(entry: Any, *names: str) -> Any:
    if isinstance(entry, Mapping):
        lowered = {str(key).lower(): value for key, value in entry.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        return None
    fields = getattr(entry, "fields", None)
    if isinstance(fields, Mapping):
        lowered = {str(key).lower(): value for key, value in fields.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
    for name in names:
        if hasattr(entry, name):
            return getattr(entry, name)
    return None


def _candidate_doi(candidate: Candidate) -> str:
    values = (
        candidate.identifier,
        candidate.url,
        str((candidate.raw or {}).get("doi", "")),
        str((candidate.raw or {}).get("DOI", "")),
    )
    for value in values:
        match = _DOI_RE.search(value)
        if match:
            return match.group().rstrip(".,;)")
    return ""


def _candidate_arxiv(candidate: Candidate) -> str:
    values = (
        candidate.identifier,
        candidate.url,
        str((candidate.raw or {}).get("arxiv_id", "")),
        str((candidate.raw or {}).get("arxiv", "")),
        str((candidate.raw or {}).get("eprint", "")),
    )
    for value in values:
        match = _ARXIV_RE.search(value)
        if match:
            return match.group("identifier")
    return ""


def _normalize_doi(value: str) -> str:
    return re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    ).casefold().rstrip(".,;)")


def _normalize_arxiv(value: str) -> str:
    match = _ARXIV_RE.search(value)
    if match:
        return match.group("identifier").casefold()
    return re.sub(r"v\d+$", "", value.strip(), flags=re.IGNORECASE).casefold()


def _normalize_text(value: str) -> str:
    value = _decode_latex(value).replace("&", " and ")
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _title_similarity(left: str, right: str) -> float:
    left, right = _normalize_text(left), _normalize_text(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if not union:
        return 0.0
    jaccard = overlap / union
    containment = overlap / min(len(left_tokens), len(right_tokens))
    return max(
        SequenceMatcher(None, left, right).ratio(),
        0.55 * jaccard + 0.45 * containment,
    )


def _year_similarity(expected: int | None, actual: int | None) -> float | None:
    if expected is None or actual is None:
        return None
    difference = abs(expected - actual)
    return 1.0 if difference == 0 else 0.7 if difference == 1 else 0.0


def _years_compatible(left: int | None, right: int | None) -> bool:
    return left is None or right is None or abs(left - right) <= 1


def _is_truncation_marker(author: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", _normalize_text(author))
    return normalized in _TRUNCATION_MARKERS or bool(_TRUNCATION_RE.search(author))


def _author_identity(author: str) -> tuple[str, str]:
    value = _normalize_text(author)
    if not value:
        return "", ""
    if "," in author:
        family, given = author.split(",", 1)
        family_key = _normalize_text(family)
        given_key = _initials(given)
    else:
        tokens = value.split()
        family_key = tokens[-1]
        given_key = _initials(" ".join(tokens[:-1]))
    return family_key, given_key


def _initials(value: str) -> str:
    return "".join(token[0] for token in _normalize_text(value).split() if token)


def _author_pair_score(left: str, right: str) -> float:
    left_family, left_initials = _author_identity(left)
    right_family, right_initials = _author_identity(right)
    if not left_family or not right_family:
        return 0.0
    family_match = left_family == right_family or (
        left_family.split()[-1] == right_family.split()[-1]
        and len(left_family.split()) == len(right_family.split()) == 1
    )
    if not family_match:
        return 0.0
    if left_initials and right_initials:
        if left_initials == right_initials:
            return 1.0
        if left_initials.startswith(right_initials) or right_initials.startswith(
            left_initials
        ):
            return 0.92
        return 0.0
    return 0.78


def _match_authors(
    expected: Sequence[str],
    actual: Sequence[str],
) -> list[tuple[int, int]]:
    # 先匹配相似度最高的姓名组合，而不是贪心地采用第一个同姓结果。
    # 当作者中出现重复姓氏或姓名缩写时，这一点尤其重要。
    edges = [
        (_author_pair_score(left, right), left_index, right_index)
        for left_index, left in enumerate(expected)
        for right_index, right in enumerate(actual)
        if _author_pair_score(left, right) > 0
    ]
    edges.sort(key=lambda item: (-item[0], item[1], item[2]))
    pairs: list[tuple[int, int]] = []
    used_expected: set[int] = set()
    used_actual: set[int] = set()
    for _, left_index, right_index in edges:
        if left_index in used_expected or right_index in used_actual:
            continue
        pairs.append((left_index, right_index))
        used_expected.add(left_index)
        used_actual.add(right_index)
    return sorted(pairs)


def _reordered_authors(
    pairs: Sequence[tuple[int, int]],
    actual: Sequence[str],
) -> list[str]:
    if len(pairs) < 2:
        return []
    ordered = sorted(pairs)
    actual_positions = [right for _, right in ordered]
    if actual_positions == sorted(actual_positions):
        return []
    return [
        f"{_display_author(actual[right])}（Bib第{left + 1}位→检索第{right + 1}位）"
        for left, right in ordered
    ]


def _display_author(author: str) -> str:
    value = _decode_latex(author)
    value = " ".join(re.sub(r"[{}]", "", value).split())
    if "," in value:
        family, given = (part.strip() for part in value.split(",", 1))
        return " ".join(part for part in (given, family) if part)
    return value


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
            "NFC",
            letter + accents[match.group("accent")],
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


__all__ = [
    "CandidateAssessment",
    "CandidateGroup",
    "assess_candidate",
    "compare_authors",
    "consolidate_candidates",
    "rank_candidates",
]
