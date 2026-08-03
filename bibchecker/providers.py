"""轻量的 OpenAlex、Crossref、arXiv 与离线元数据来源。"""

from __future__ import annotations

import json
import os
import re
import threading
import gzip
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .models import BibEntry, Candidate
from .parser import (
    _iter_bib_blocks,
    _parse_value,
    _split_assignment,
    _split_entry_content,
    _split_top_level,
    _strip_percent_comments,
)


ARXIV_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


class Provider:
    name = "provider"
    definitive = False
    authoritative = False
    identifier_lookup = False
    academic_source = True

    def applies(self, entry: Any) -> bool:
        return True

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        return None

    def search_title(self, entry: Any) -> list[Candidate]:
        return []

    def search(self, entry: Any) -> list[Candidate]:
        identifier = self.lookup_identifier(entry)
        return identifier if identifier else self.search_title(entry)


class _HTTPProvider(Provider):
    _cache: dict[tuple[str, str], bytes] = {}
    _request_locks: dict[tuple[str, str], threading.Lock] = {}
    _cache_lock = threading.Lock()

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_results: int = 5,
        email: str | None = None,
        token: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_results = max_results
        self.email = email
        self.token = token

    def _get(self, url: str, accept: str = "application/json") -> bytes:
        cache_key = (url, accept)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            request_lock = self._request_locks.setdefault(
                cache_key, threading.Lock()
            )
        if cached is not None:
            return cached

        with request_lock:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            user_agent = "Paper-BibChecker/0.1"
            if self.email:
                user_agent += f" (mailto:{self.email})"
            headers = self._headers(accept, user_agent)
            request = Request(url, headers=headers)
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            with self._cache_lock:
                self._cache[cache_key] = body
            return body

    def _json(self, url: str) -> Mapping[str, Any]:
        return json.loads(self._get(url).decode("utf-8"))

    def _headers(self, accept: str, user_agent: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Accept-Encoding": "gzip",
            "User-Agent": user_agent,
        }
        if self.email:
            headers["From"] = self.email
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


class DirectURLProvider(_HTTPProvider):
    """直接从 Bib 中的显式 URL 读取引用元数据。"""

    name = "url"
    academic_source = False

    def applies(self, entry: Any) -> bool:
        url = _entry_url(entry)
        return bool(url and "github.com" not in url.casefold())

    def search_title(self, entry: Any) -> list[Candidate]:
        url = _entry_url(entry)
        if not url or url.casefold().split("?", 1)[0].endswith(".pdf"):
            return []
        text = self._get(url, accept="text/html").decode("utf-8", "ignore")
        if text.lstrip().startswith("%PDF-"):
            return []

        metadata = _html_metadata(text)
        title = _first_metadata(
            metadata,
            "citation_title",
            "dc.title",
            "dcterms.title",
            "og:title",
            "twitter:title",
        )
        if not title:
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL
            )
            title = _strip_html(title_match.group(1)) if title_match else ""
        if "notion.site" in url.casefold():
            title = re.sub(r"\s*\|\s*Notion\s*$", "", title, flags=re.I)
        if not title:
            return []

        authors = (
            metadata.get("citation_author")
            or metadata.get("dc.creator")
            or metadata.get("dcterms.creator")
            or metadata.get("author")
            or []
        )
        if not authors and "notion.site" in url.casefold():
            authors = _notion_authors(text)
        date = _first_metadata(
            metadata,
            "citation_publication_date",
            "citation_date",
            "dc.date",
            "dcterms.date",
            "date",
        )
        if not date and "notion.site" in url.casefold():
            date = _notion_date(text)
        venue = _first_metadata(
            metadata,
            "citation_conference_title",
            "citation_journal_title",
            "dc.source",
        )
        is_notion = "notion.site" in url.casefold()
        if is_notion and not venue:
            venue = "Notion Blog"
        doi = _first_metadata(metadata, "citation_doi", "dc.identifier")
        arxiv_id = _extract_arxiv(url, doi)
        return [
            Candidate(
                source="notion" if is_notion else self.name,
                title=unescape(title).strip(),
                authors=[unescape(author).strip() for author in authors],
                year=_year(date),
                venue=unescape(venue).strip(),
                url=url,
                identifier=_extract_doi(doi) or arxiv_id or url,
                raw={
                    **({"doi": _extract_doi(doi)} if _extract_doi(doi) else {}),
                    **({"arxiv_id": arxiv_id} if arxiv_id else {}),
                },
            )
        ]


class ArxivProvider(_HTTPProvider):
    name = "arxiv"
    identifier_lookup = True
    endpoint = "https://export.arxiv.org/api/query"
    abs_endpoint = "https://arxiv.org/abs"
    atom = {"atom": "http://www.w3.org/2005/Atom"}

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        arxiv_id = _entry_arxiv(entry)
        if not arxiv_id:
            return None
        normalized = _normalize_arxiv(arxiv_id)
        # export.arxiv.org 的 API 在并行检查较多条目时容易限流或超时。
        # abs 页面同样提供标准 citation_* 元数据，而且对并行只读请求更稳定。
        # 同时读取最新版本和 v1，避免论文改名或作者变更造成误报。
        identifiers = [normalized, f"{normalized}v1"]
        candidates: list[Candidate] = []
        errors: list[Exception] = []

        def fetch(identifier: str) -> Candidate:
            return self._abs_candidate(identifier)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fetch, identifier) for identifier in identifiers]
            for future in futures:
                try:
                    candidates.append(future.result())
                except Exception as error:
                    errors.append(error)

        if candidates:
            return candidates

        # abs 页面若整体不可用，再退回 Atom API；正常路径不会触发该请求。
        try:
            return self._query(
                {"id_list": f"{normalized},{normalized}v1", "max_results": 2},
                arxiv_id,
            )
        except Exception:
            if errors:
                raise errors[0]
            raise

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        return self._query(
            {
                "search_query": f'ti:"{title.replace(chr(34), " ")}"',
                "start": 0,
                "max_results": self.max_results,
            },
            "",
        )

    def _query(
        self, params: Mapping[str, Any], requested_arxiv_id: str
    ) -> list[Candidate]:
        root = ET.fromstring(
            self._get(
                f"{self.endpoint}?{urlencode(params)}", accept="application/atom+xml",
            )
        )
        candidates: list[Candidate] = []
        for item in root.findall("atom:entry", self.atom):
            url = _element_text(item.find("atom:id", self.atom))
            title_value = _element_text(item.find("atom:title", self.atom))
            authors = [
                _element_text(author.find("atom:name", self.atom))
                for author in item.findall("atom:author", self.atom)
            ]
            published = _element_text(item.find("atom:published", self.atom))
            year = _year(published)
            doi = _element_text(item.find("{http://arxiv.org/schemas/atom}doi"))
            identifier = _extract_arxiv(url) or _normalize_arxiv(requested_arxiv_id)
            candidates.append(
                Candidate(
                    source=self.name,
                    title=title_value,
                    authors=authors,
                    year=year,
                    venue="arXiv",
                    url=url,
                    identifier=identifier,
                    raw={
                        **({"doi": doi} if doi else {}),
                        "arxiv_version": _extract_arxiv_version(url),
                    },
                )
            )
        return candidates

    def _abs_candidate(self, identifier: str) -> Candidate:
        url = f"{self.abs_endpoint}/{quote(identifier, safe='/')}"
        text = self._get(url, accept="text/html").decode("utf-8", "ignore")
        metadata = _html_metadata(text)
        title = _first_metadata(metadata, "citation_title")
        if not title:
            raise ValueError(f"arXiv 页面未提供引用元数据：{identifier}")
        arxiv_id = (
            _first_metadata(metadata, "citation_arxiv_id")
            or _normalize_arxiv(identifier)
        )
        doi = _first_metadata(metadata, "citation_doi")
        return Candidate(
            source=self.name,
            title=title,
            authors=list(metadata.get("citation_author") or []),
            year=_year(
                _first_metadata(
                    metadata,
                    "citation_date",
                    "citation_online_date",
                )
            ),
            venue="arXiv",
            url=url,
            identifier=_normalize_arxiv(arxiv_id),
            raw={
                **({"doi": _extract_doi(doi)} if _extract_doi(doi) else {}),
                "arxiv_version": _extract_arxiv_version(identifier),
            },
        )


ArXivProvider = ArxivProvider


class DataCiteProvider(_HTTPProvider):
    """读取 DataCite 中的 arXiv DOI 记录，用作稳定标识符的回退来源。"""

    name = "datacite"
    identifier_lookup = True
    endpoint = "https://api.datacite.org/dois"

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        params = {
            "query": f'titles.title:"{title}"',
            "page[size]": self.max_results,
        }
        records = self._json(
            f"{self.endpoint}?{urlencode(params)}"
        ).get("data", [])
        candidates: list[Candidate] = []
        for record in records:
            attributes = record.get("attributes") or {}
            titles = attributes.get("titles") or []
            title_value = titles[0].get("title", "") if titles else ""
            creators = attributes.get("creators") or []
            authors = [
                str(creator.get("name") or "").strip()
                for creator in creators
                if creator.get("name")
            ]
            candidates.append(
                Candidate(
                    source=self.name,
                    title=title_value,
                    authors=authors,
                    year=_year(attributes.get("publicationYear")),
                    venue=str(attributes.get("publisher") or ""),
                    url=str(attributes.get("url") or ""),
                    identifier=str(attributes.get("doi") or ""),
                    raw=dict(attributes),
                )
            )
        return _sort_title_candidates(title, candidates)[: self.max_results]

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        arxiv_id = _entry_arxiv(entry)
        if not arxiv_id:
            return None
        doi = f"10.48550/arxiv.{_normalize_arxiv(arxiv_id)}"
        try:
            record = self._json(f"{self.endpoint}/{quote(doi, safe='')}").get(
                "data", {}
            )
        except HTTPError as error:
            if error.code == 404:
                return []
            raise
        attributes = record.get("attributes") or {}
        title = (attributes.get("titles") or [{}])[0].get("title", "")
        creators = attributes.get("creators") or []
        authors = [
            str(creator.get("name") or "").strip()
            for creator in creators
            if creator.get("name")
        ]
        return [
            Candidate(
                source=self.name,
                title=title,
                authors=authors,
                year=_year(attributes.get("publicationYear")),
                venue=str(attributes.get("publisher") or ""),
                url=str(attributes.get("url") or ""),
                identifier=_normalize_arxiv(arxiv_id),
                raw=dict(attributes),
            )
        ]


class OpenAlexProvider(_HTTPProvider):
    name = "openalex"
    identifier_lookup = True
    endpoint = "https://api.openalex.org/works"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("email", os.environ.get("OPENALEX_EMAIL"))
        super().__init__(**kwargs)

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        doi = _entry_doi(entry)
        if not doi:
            return None
        try:
            records = [
                self._json(f"{self.endpoint}/https://doi.org/{quote(_normalize_doi(doi), safe='')}")
            ]
        except HTTPError as error:
            if error.code != 404:
                raise
            records = []
        return self._candidates(records, _entry_title(entry))

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        params = {
            "search": title,
            "per-page": self.max_results,
            "select": (
                "id,display_name,publication_year,authorships,doi,ids,"
                "primary_location,locations,type"
            ),
        }
        api_key = os.environ.get("OPENALEX_API_KEY")
        if api_key:
            params["api_key"] = api_key
        records = self._json(f"{self.endpoint}?{urlencode(params)}").get("results", [])
        return self._candidates(records, title)

    def _candidates(
        self,
        records: Sequence[Mapping[str, Any]],
        title: str,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for item in records:
            location = item.get("primary_location") or {}
            source = location.get("source") or {}
            authors = [
                authorship.get("raw_author_name")
                or (authorship.get("author") or {}).get("display_name", "")
                for authorship in item.get("authorships") or []
            ]
            ids = item.get("ids") or {}
            doi_value = _extract_doi(item.get("doi"), ids.get("doi"))
            arxiv_id = _extract_arxiv(
                ids.get("arxiv"),
                location.get("landing_page_url"),
                *[
                    candidate.get("landing_page_url") for candidate in item.get("locations") or []
                ],
            )
            candidates.append(
                Candidate(
                    source=self.name,
                    title=item.get("display_name") or item.get("title") or "",
                    authors=[author for author in authors if author],
                    year=_year(item.get("publication_year")),
                    venue=source.get("display_name") or "",
                    url=location.get("landing_page_url")
                    or item.get("doi")
                    or item.get("id")
                    or "",
                    identifier=doi_value or arxiv_id or "",
                    raw=dict(item),
                )
            )
        return _sort_title_candidates(title, candidates)[: self.max_results]


class CrossrefProvider(_HTTPProvider):
    name = "crossref"
    identifier_lookup = True
    endpoint = "https://api.crossref.org/works"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("email", os.environ.get("CROSSREF_EMAIL"))
        super().__init__(**kwargs)

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        doi = _entry_doi(entry)
        if not doi:
            return None
        try:
            records = [
                self._json(f"{self.endpoint}/{quote(_normalize_doi(doi), safe='')}").get("message", {})
            ]
        except HTTPError as error:
            if error.code != 404:
                raise
            records = []
        return self._candidates(records)

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        params = {
            "query.bibliographic": title,
            **(
                {"query.author": first_author}
                if (first_author := _entry_first_author(entry))
                else {}
            ),
            "rows": self.max_results,
            "select": "DOI,title,author,published,published-print,"
            "published-online,URL,container-title",
        }
        records = (
            self._json(f"{self.endpoint}?{urlencode(params)}").get("message", {}).get("items", [])
        )
        return self._candidates(records)

    def _candidates(
        self, records: Sequence[Mapping[str, Any]]
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for item in records:
            title_value = item.get("title") or [""]
            container = item.get("container-title") or [""]
            authors = [
                " ".join(
                    part for part in (author.get("given"), author.get("family")) if part
                )
                for author in item.get("author") or []
            ]
            doi_value = _extract_doi(item.get("DOI"))
            candidates.append(
                Candidate(
                    source=self.name,
                    title=title_value[0]
                    if isinstance(title_value, list)
                    else str(title_value),
                    authors=[author for author in authors if author],
                    year=_crossref_year(item),
                    venue=container[0]
                    if isinstance(container, list)
                    else str(container),
                    url=item.get("URL") or "",
                    identifier=doi_value or _extract_arxiv(item.get("URL")) or "",
                    raw=dict(item),
                )
            )
        return candidates


class SemanticScholarProvider(_HTTPProvider):
    """使用 Semantic Scholar Academic Graph API 查询论文元数据。"""

    name = "semanticscholar"
    identifier_lookup = True
    endpoint = "https://api.semanticscholar.org/graph/v1/paper"
    fields = "title,authors,year,venue,publicationVenue,externalIds,url"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault(
            "token",
            os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
            or os.environ.get("S2_API_KEY"),
        )
        super().__init__(**kwargs)

    def _headers(self, accept: str, user_agent: str) -> dict[str, str]:
        headers = super()._headers(accept, user_agent)
        token = headers.pop("Authorization", "")
        if token.startswith("Bearer "):
            headers["x-api-key"] = token.removeprefix("Bearer ")
        return headers

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        identifiers: list[str] = []
        if doi := _entry_doi(entry):
            identifiers.append(f"DOI:{_normalize_doi(doi)}")
        if arxiv_id := _entry_arxiv(entry):
            identifiers.append(f"ARXIV:{_normalize_arxiv(arxiv_id)}")
        if not identifiers:
            return None

        candidates: list[Candidate] = []
        for identifier in identifiers:
            try:
                record = self._json(
                    f"{self.endpoint}/{quote(identifier, safe=':')}?"
                    f"{urlencode({'fields': self.fields})}"
                )
            except HTTPError as error:
                if error.code == 404:
                    continue
                raise
            candidates.append(self._candidate(record))
        return candidates

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        payload = self._json(
            f"{self.endpoint}/search/match?"
            f"{urlencode({'query': title, 'fields': self.fields})}"
        )
        records = payload.get("data") or []
        if isinstance(records, Mapping):
            records = [records]
        return _sort_title_candidates(
            title,
            [self._candidate(record) for record in records],
        )[: self.max_results]

    def _candidate(self, item: Mapping[str, Any]) -> Candidate:
        external_ids = item.get("externalIds") or {}
        publication_venue = item.get("publicationVenue") or {}
        authors = [
            str(author.get("name") or "").strip()
            for author in item.get("authors") or []
            if author.get("name")
        ]
        doi = _extract_doi(external_ids.get("DOI"))
        arxiv_id = _extract_arxiv(external_ids.get("ArXiv"))
        venue = str(
            item.get("venue")
            or publication_venue.get("name")
            or ""
        )
        return Candidate(
            source=self.name,
            title=str(item.get("title") or ""),
            authors=authors,
            year=_year(item.get("year")),
            venue=venue,
            url=str(item.get("url") or ""),
            identifier=doi or arxiv_id or str(item.get("paperId") or ""),
            raw=dict(item),
        )


class DBLPProvider(_HTTPProvider):
    """DBLP 文献搜索，仅执行标题查询。"""

    name = "dblp"
    endpoint = "https://dblp.org/search/publ/api"

    def applies(self, entry: Any) -> bool:
        # DBLP 对示例中的批量标题搜索经常主动断开连接；对于已能识别
        # 会议/期刊的条目，官方来源以及 OpenAlex/Crossref 已提供更直接
        # 的证据，因此只将 DBLP 用于没有稳定标识符且没有明确 venue 的条目。
        return (
            not _entry_arxiv(entry)
            and not _entry_doi(entry)
            and not _venue_text(entry).strip()
        )

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        params = {"q": title, "format": "json", "h": self.max_results, "c": 0}
        payload = self._json(f"{self.endpoint}?{urlencode(params)}")
        result = payload.get("result") or {}
        hits = (result.get("hits") or {}).get("hit") or []
        if isinstance(hits, Mapping):
            hits = [hits]
        return [self._candidate(hit.get("info") or {}) for hit in hits]

    def _candidate(self, info: Mapping[str, Any]) -> Candidate:
        authors_value = (info.get("authors") or {}).get("author") or []
        if isinstance(authors_value, Mapping):
            authors_value = [authors_value]
        authors = [
            str(author.get("text") or author.get("name") or "")
            if isinstance(author, Mapping)
            else str(author)
            for author in authors_value
        ]
        authors = [
            re.sub(r"\s+\d{4}(?:-\d{4})?$", "", author).strip() for author in authors
        ]
        ee = info.get("ee") or ""
        if isinstance(ee, list):
            ee = ee[0] if ee else ""
        doi = _extract_doi(info.get("doi"), ee)
        return Candidate(
            source=self.name,
            title=str(info.get("title") or ""),
            authors=[author for author in authors if author],
            year=_year(info.get("year")),
            venue=str(info.get("venue") or ""),
            url=str(info.get("url") or ee),
            identifier=doi or str(info.get("key") or ""),
            raw=dict(info),
        )


class _BookProceedingsProvider(_HTTPProvider):
    """搜索 ICLR/NeurIPS 官方论文集。"""

    authoritative = True
    endpoint = ""
    venue = ""
    venue_markers: tuple[str, ...] = ()

    def applies(self, entry: Any) -> bool:
        return _venue_matches(entry, self.venue_markers)

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        text = self._get(
            f"{self.endpoint}/papers/search?{urlencode({'q': title})}",
            accept="text/html",
        ).decode("utf-8", "ignore")
        candidates: list[Candidate] = []
        pattern = re.compile(
            r"<li[^>]*>\s*\((?P<year>\d{4})\)\s*"
            r"<a[^>]+href=[\"'](?P<url>[^\"']+)[\"'][^>]*>"
            r"(?P<title>.*?)</a>\s*(?P<authors>.*?)</li>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(text):
            author_text = _strip_html(match.group("authors"))
            url = urljoin(self.endpoint, unescape(match.group("url")))
            candidates.append(
                Candidate(
                    source=self.name,
                    title=_strip_html(match.group("title")),
                    authors=_split_display_authors(author_text),
                    year=_year(match.group("year")),
                    venue=self.venue,
                    url=url,
                    identifier=url.rsplit("/", 1)[-1].split("-", 1)[0],
                    raw={},
                )
            )
        return _sort_title_candidates(title, candidates)[: self.max_results]


class ICLRProceedingsProvider(_BookProceedingsProvider):
    name = "iclr"
    endpoint = "https://proceedings.iclr.cc"
    venue = "International Conference on Learning Representations"
    venue_markers = (
        "iclr",
        "international conference on learning representations",
        "proceedings.iclr.cc",
    )


class NeurIPSProceedingsProvider(_BookProceedingsProvider):
    name = "neurips"
    endpoint = "https://papers.nips.cc"
    venue = "Advances in Neural Information Processing Systems"
    venue_markers = (
        "neurips",
        "nips",
        "neural information processing systems",
        "papers.nips.cc",
    )


class ICMLProceedingsProvider(_HTTPProvider):
    """搜索 PMLR 中的 ICML 官方论文集卷。"""

    name = "icml"
    endpoint = "https://proceedings.mlr.press"
    authoritative = True

    def applies(self, entry: Any) -> bool:
        return _venue_matches(
            entry,
            ("icml", "international conference on machine learning"),
        )

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        year = _year(_value(entry, "year"))
        if not title or not year:
            return []

        candidates: list[Candidate] = []
        searched = False
        last_error: ValueError | None = None
        for publication_year in (year, year - 1):
            try:
                year_candidates = self._search_year(publication_year)
            except ValueError as error:
                last_error = error
                continue
            searched = True
            candidates.extend(year_candidates)
            if _has_close_title(title, year_candidates):
                break
        if not searched and last_error:
            raise last_error
        return _sort_title_candidates(title, candidates)[: self.max_results]

    def _search_year(self, year: int) -> list[Candidate]:
        volume = self._volume_for_year(year)
        text = self._get(
            f"{self.endpoint}/v{volume}/", accept="text/html"
        ).decode("utf-8", "ignore")
        candidates: list[Candidate] = []
        pattern = re.compile(
            r"<div class=[\"']paper[\"']>\s*"
            r"<p class=[\"']title[\"']>(?P<title>.*?)</p>.*?"
            r"<span class=[\"']authors[\"']>(?P<authors>.*?)</span>.*?"
            r"<a href=[\"'](?P<url>[^\"']+\.html)[\"'][^>]*>abs</a>.*?"
            r"</div>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(text):
            url = urljoin(f"{self.endpoint}/v{volume}/", match.group("url"))
            candidates.append(
                Candidate(
                    source=self.name,
                    title=_strip_html(match.group("title")),
                    authors=_split_display_authors(
                        _strip_html(match.group("authors"))
                    ),
                    year=year,
                    venue="International Conference on Machine Learning",
                    url=url,
                    identifier=url.rsplit("/", 1)[-1].removesuffix(".html"),
                    raw={"volume": volume},
                )
            )
        return candidates

    def _volume_for_year(self, year: int) -> int:
        known_volumes = {
            2013: 28,
            2014: 32,
            2015: 37,
            2016: 48,
            2017: 70,
            2018: 80,
            2019: 97,
            2020: 119,
            2021: 139,
            2022: 162,
            2023: 202,
            2024: 235,
            2025: 267,
        }
        if year in known_volumes:
            return known_volumes[year]
        text = self._get(f"{self.endpoint}/", accept="text/html").decode(
            "utf-8", "ignore"
        )
        pattern = re.compile(
            r"<li>\s*<a href=[\"']?v(?P<volume>\d+)[\"']?[^>]*>.*?</a>"
            r"(?P<label>.*?)</li>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(text):
            label = _title_key(_strip_html(match.group("label")))
            if str(year) not in label:
                continue
            if "proceedings of icml" in label or (
                "international conference on machine learning" in label
            ):
                return int(match.group("volume"))
        raise ValueError(f"未找到 ICML {year} 对应的官方 PMLR 卷")


def _parse_anthology_bib(
    text: str,
    *,
    source: str,
    venue: str,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for entry_type, content in _iter_bib_blocks(_strip_percent_comments(text)):
        if entry_type not in {"article", "inproceedings", "incollection"}:
            continue
        key, fields_text = _split_entry_content(content)
        fields: dict[str, str] = {}
        for assignment in _split_top_level(fields_text, ","):
            name, expression = _split_assignment(assignment)
            if name:
                fields[name.casefold()] = _parse_value(expression, lambda _: None)
        title = fields.get("title", "")
        if not title:
            continue
        candidates.append(
            Candidate(
                source=source,
                title=title,
                authors=[
                    part.strip()
                    for part in re.split(
                        r"\s+and\s+", fields.get("author", ""), flags=re.I
                    )
                    if part.strip()
                ],
                year=_year(fields.get("year")),
                venue=venue,
                url=f"https://aclanthology.org/{key}/",
                identifier=key,
                raw=fields,
            )
        )
    return candidates


class _ACLVenueProvider(_HTTPProvider):
    """搜索会议页面所列的现代 ACL Anthology 官方论文集卷。"""

    authoritative = True
    endpoint = "https://aclanthology.org"
    venue_id = ""
    venue = ""
    venue_markers: tuple[str, ...] = ()

    def applies(self, entry: Any) -> bool:
        year = _year(_value(entry, "year"))
        venue_text = _venue_text(entry)
        return bool(
            year
            and "findings" not in venue_text
            and _venue_matches(entry, self.venue_markers)
        )

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        year = _year(_value(entry, "year"))
        if not title or not year:
            return []
        page = self._get(
            f"{self.endpoint}/venues/{self.venue_id}/", accept="text/html"
        ).decode("utf-8", "ignore")
        volume_ids = _anthology_year_volume_ids(page, self.venue_id, year)
        if not volume_ids:
            volume_ids = list(
                dict.fromkeys(
                    volume_id
                    for volume_id in re.findall(
                        r"href=[\"']?/volumes/([^/\"' >]+)/?",
                        page,
                        flags=re.IGNORECASE,
                    )
                    if volume_id.casefold().startswith(
                        f"{year}.{self.venue_id}-"
                    )
                )
            )
        if not volume_ids:
            raise ValueError(
                f"未找到 {self.venue_id.upper()} {year} 对应的官方论文集卷"
            )
        candidates: list[Candidate] = []
        for volume_id in volume_ids:
            text = self._get(
                f"{self.endpoint}/volumes/{volume_id}.bib",
                accept="text/x-bibtex",
            ).decode("utf-8", "ignore")
            candidates.extend(
                _parse_anthology_bib(
                    text,
                    source=self.name,
                    venue=self.venue,
                )
            )
        return _sort_title_candidates(title, candidates)[: self.max_results]


class ACLProceedingsProvider(_ACLVenueProvider):
    name = "acl"
    venue_id = "acl"
    venue = "Annual Meeting of the Association for Computational Linguistics"
    venue_markers = (
        "acl",
        "annual meeting of the association for computational linguistics",
    )


class EMNLPProceedingsProvider(_ACLVenueProvider):
    name = "emnlp"
    venue_id = "emnlp"
    venue = "Conference on Empirical Methods in Natural Language Processing"
    venue_markers = (
        "emnlp",
        "conference on empirical methods in natural language processing",
    )


class _CVFProceedingsProvider(_HTTPProvider):
    """读取 CVF Open Access 中的 CVPR/ICCV 官方论文列表。"""

    authoritative = True
    endpoint = "https://openaccess.thecvf.com"
    conference = ""
    venue = ""
    venue_markers: tuple[str, ...] = ()

    def applies(self, entry: Any) -> bool:
        return _venue_matches(entry, self.venue_markers)

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        year = _year(_value(entry, "year"))
        if not title or not year:
            return []
        text = self._get(
            f"{self.endpoint}/{self.conference}{year}?day=all",
            accept="text/html",
        ).decode("utf-8", "ignore")
        candidates: list[Candidate] = []
        pattern = re.compile(
            r"<dt[^>]*class=[\"']ptitle[\"'][^>]*>.*?"
            r"<a href=[\"']?(?P<url>[^\"' >]+_paper\.html)[\"']?[^>]*>"
            r"(?P<title>.*?)</a>.*?</dt>\s*"
            r"<dd>(?P<authors>.*?)</dd>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(text):
            authors = re.findall(
                r"<input[^>]+name=[\"']query_author[\"'][^>]+"
                r"value=[\"'](?P<author>[^\"']+)[\"']",
                match.group("authors"),
                flags=re.IGNORECASE,
            )
            url = urljoin(self.endpoint, unescape(match.group("url")))
            candidates.append(
                Candidate(
                    source=self.name,
                    title=_strip_html(match.group("title")),
                    authors=[unescape(author).strip() for author in authors],
                    year=year,
                    venue=self.venue,
                    url=url,
                    identifier=url.rsplit("/", 1)[-1].removesuffix(".html"),
                    raw={},
                )
            )
        return _sort_title_candidates(title, candidates)[: self.max_results]


class CVPRProceedingsProvider(_CVFProceedingsProvider):
    name = "cvpr"
    conference = "CVPR"
    venue = "IEEE/CVF Conference on Computer Vision and Pattern Recognition"
    venue_markers = (
        "cvpr",
        "conference on computer vision and pattern recognition",
    )


class ICCVProceedingsProvider(_CVFProceedingsProvider):
    name = "iccv"
    conference = "ICCV"
    venue = "IEEE/CVF International Conference on Computer Vision"
    venue_markers = (
        "iccv",
        "international conference on computer vision",
    )


class ECCVProceedingsProvider(_HTTPProvider):
    """读取欧洲计算机视觉协会维护的 ECCV 官方论文存档。"""

    name = "eccv"
    endpoint = "https://www.ecva.net/papers.php"
    authoritative = True

    def applies(self, entry: Any) -> bool:
        return _venue_matches(
            entry,
            ("eccv", "european conference on computer vision"),
        )

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        year = _year(_value(entry, "year"))
        if not title or not year:
            return []
        text = self._get(self.endpoint, accept="text/html").decode(
            "utf-8", "ignore"
        )
        section_match = re.search(
            rf"<!--\s*ECCV\s+{year}\s*-->"
            rf"(?P<section>.*?)(?=<!--\s*ECCV\s+\d{{4}}\s*-->|$)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            raise ValueError(f"未找到 ECCV {year} 对应的官方论文区段")
        candidates: list[Candidate] = []
        pattern = re.compile(
            r"<dt[^>]*class=[\"']ptitle[\"'][^>]*>.*?"
            r"<a href=[\"']?(?P<url>[^\"' >]+)[\"']?[^>]*>"
            r"(?P<title>.*?)</a>.*?</dt>\s*"
            r"<dd>(?P<authors>.*?)</dd>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(section_match.group("section")):
            author_text = re.sub(r"\*+", "", _strip_html(match.group("authors")))
            url = urljoin(self.endpoint, unescape(match.group("url")))
            candidates.append(
                Candidate(
                    source=self.name,
                    title=_strip_html(match.group("title")),
                    authors=_split_display_authors(author_text),
                    year=year,
                    venue="European Conference on Computer Vision",
                    url=url,
                    identifier=url.rsplit("/", 1)[-1].removesuffix(".php"),
                    raw={},
                )
            )
        return _sort_title_candidates(title, candidates)[: self.max_results]


class ACLAnthologyProvider(_HTTPProvider):
    """使用 ACL Anthology 官方 BibTeX 检查 TACL 卷。"""

    name = "acl_anthology"
    endpoint = "https://aclanthology.org/volumes"
    authoritative = True

    def applies(self, entry: Any) -> bool:
        venue = str(_value(entry, "journal", "venue") or "").casefold()
        return "tacl" in venue or "transactions of the association" in venue

    def search_title(self, entry: Any) -> list[Candidate]:
        year = _year(_value(entry, "year"))
        if not year:
            return []
        url = f"{self.endpoint}/{year}.tacl-1.bib"
        text = self._get(url, accept="text/plain").decode("utf-8")
        return _parse_anthology_bib(
            text,
            source=self.name,
            venue="TACL",
        )


class JMLRProvider(_HTTPProvider):
    """用 JMLR 官方卷索引核验没有 DOI 的 JMLR 引用。"""

    name = "jmlr"
    endpoint = "https://jmlr.org/papers"
    authoritative = True

    def applies(self, entry: Any) -> bool:
        venue = str(_value(entry, "journal", "venue") or "").casefold()
        return "jmlr" in venue or "journal of machine learning research" in venue

    def search_title(self, entry: Any) -> list[Candidate]:
        volume = str(_value(entry, "volume") or "").strip()
        if not volume.isdigit():
            return []
        text = self._get(f"{self.endpoint}/v{volume}/", accept="text/html").decode(
            "utf-8", "ignore"
        )
        candidates: list[Candidate] = []
        pattern = re.compile(
            r"<dt>(?P<title>.*?)</dt>\s*<dd>\s*<b><i>(?P<authors>.*?)</i></b>;"
            r".*?href=['\"](?P<abstract>/papers/v\d+/[^'\"]+\.html)['\"]",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(text):
            title = _strip_html(match.group("title"))
            author_text = _strip_html(match.group("authors"))
            authors = [
                part.strip()
                for part in re.split(r"\s*,\s*|\s+and\s+", author_text)
                if part.strip()
            ]
            abstract = match.group("abstract")
            year = _year(match.group(0))
            candidates.append(
                Candidate(
                    source=self.name,
                    title=title,
                    authors=authors,
                    year=year,
                    venue="Journal of Machine Learning Research",
                    url=f"https://jmlr.org{abstract}",
                    identifier=abstract.rsplit("/", 1)[-1].removesuffix(".html"),
                    raw={},
                )
            )
        return candidates


class OpenReviewProvider(_HTTPProvider):
    """搜索 OpenReview，主要覆盖 ICLR 和 COLM 投稿。"""

    name = "openreview"
    authoritative = True
    endpoint = "https://api2.openreview.net/notes/search"

    def applies(self, entry: Any) -> bool:
        venue = str(
            _value(entry, "booktitle", "journal", "venue", "url") or ""
        ).casefold()
        return any(
            marker in venue
            for marker in (
                "conference on language modeling",
                "international conference on learning representations",
                "colm",
                "iclr",
                "openreview.net",
            )
        )

    def search_title(self, entry: Any) -> list[Candidate]:
        title = _entry_title(entry)
        if not title:
            return []
        params = {"term": title, "limit": max(10, self.max_results * 4)}
        notes = self._json(f"{self.endpoint}?{urlencode(params)}").get("notes", [])
        candidates: list[Candidate] = []
        for note in notes:
            content = note.get("content") or {}
            title_value = _openreview_value(content.get("title"))
            authors = _openreview_value(content.get("authors")) or []
            if not title_value or not isinstance(authors, list):
                continue
            venue = (
                _openreview_value(content.get("venue"))
                or _openreview_value(content.get("venueid"))
                or note.get("venueid")
                or ""
            )
            candidates.append(
                Candidate(
                    source=self.name,
                    title=str(title_value),
                    authors=[str(author) for author in authors],
                    year=(
                        _year(_openreview_value(content.get("year")))
                        or _timestamp_year(note.get("pdate") or note.get("cdate"))
                    ),
                    venue=str(venue),
                    url=f"https://openreview.net/forum?id={note.get('forum') or note.get('id')}",
                    identifier=str(note.get("forum") or note.get("id") or ""),
                    raw=dict(note),
                )
            )
            if len(candidates) >= max(10, self.max_results * 2):
                break
        return _sort_entry_candidates(entry, candidates)[: self.max_results]


class GitHubProvider(_HTTPProvider):
    """读取 GitHub 仓库元数据，仅在存在 GitHub 字段或 URL 时启用。"""

    name = "github"
    identifier_lookup = True
    academic_source = False
    endpoint = "https://api.github.com"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("token", os.environ.get("GITHUB_TOKEN"))
        super().__init__(**kwargs)

    def applies(self, entry: Any) -> bool:
        return _has_github_reference(entry)

    def lookup_identifier(self, entry: Any) -> list[Candidate] | None:
        repository = _github_repository(entry)
        if not repository:
            return None
        owner, name = repository
        try:
            item = self._json(
                f"{self.endpoint}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            )
        except HTTPError as error:
            if error.code == 404:
                return []
            raise
        return [self._candidate(item)]

    def search_title(self, entry: Any) -> list[Candidate]:
        if not _has_github_reference(entry) or _github_repository(entry):
            return []
        title = _entry_title(entry)
        if not title:
            return []
        params = {"q": title, "per_page": self.max_results}
        payload = self._json(f"{self.endpoint}/search/repositories?{urlencode(params)}")
        return [self._candidate(item) for item in payload.get("items") or []]

    def _candidate(self, item: Mapping[str, Any]) -> Candidate:
        owner = item.get("owner") or {}
        full_name = str(item.get("full_name") or "")
        return Candidate(
            source=self.name,
            title=str(item.get("name") or full_name),
            authors=[str(owner.get("login"))] if owner.get("login") else [],
            year=_year(item.get("created_at")),
            venue="GitHub",
            url=str(item.get("html_url") or ""),
            identifier=full_name,
            raw=dict(item),
        )


class LocalProvider(Provider):
    """由记录或 JSON 测试夹具支持的确定性离线数据源。"""

    definitive = True
    identifier_lookup = True

    def __init__(
        self,
        fixture: (
            str
            | os.PathLike[str]
            | Sequence[Mapping[str, Any]]
            | Mapping[str, Any]
        ),
        *,
        name: str = "local",
    ) -> None:
        self.name = name
        if isinstance(fixture, (str, os.PathLike)):
            with open(fixture, encoding="utf-8") as handle:
                fixture = json.load(handle)
        self.fixture = fixture
        self.records = _fixture_records(fixture)

    def search(self, entry: Any) -> list[Candidate]:
        records: list[Mapping[str, Any]]
        if isinstance(self.fixture, Mapping) and "records" not in self.fixture:
            records = []
            for key in _lookup_keys(entry):
                records.extend(_as_records(self.fixture.get(key)))
            records.extend(_as_records(self.fixture.get("*")))
        else:
            records = [
                record for record in self.records if _record_matches(entry, record)
            ]
        return [self._candidate(record) for record in _dedupe(records)]

    def lookup_identifier(self, entry: Any) -> list[Candidate]:
        return self.search(entry)

    def search_title(self, entry: Any) -> list[Candidate]:
        return self.search(entry)

    def _candidate(self, record: Mapping[str, Any]) -> Candidate:
        source = str(record.get("source") or record.get("provider") or self.name)
        authors = record.get("authors") or record.get("author") or []
        if isinstance(authors, str):
            authors = [
                part.strip() for part in re.split(r"\s+and\s+", authors, flags=re.I) if part.strip()
            ]
        doi = _extract_doi(record.get("doi"), record.get("url"))
        arxiv_id = _extract_arxiv(
            record.get("arxiv_id"),
            record.get("arxiv"),
            record.get("eprint"),
            record.get("url"),
        )
        return Candidate(
            source=source,
            title=str(record.get("title") or ""),
            authors=[str(author) for author in authors],
            year=_year(record.get("year")),
            venue=str(record.get("venue") or record.get("journal") or ""),
            url=str(record.get("url") or ""),
            identifier=str(record.get("identifier") or doi or arxiv_id or ""),
            raw=dict(record),
        )


FixtureProvider = LocalProvider


class FunctionProvider(Provider):
    identifier_lookup = True

    def __init__(
        self, function: Callable[[Any], list[Candidate]], name: str = "fixture"
    ) -> None:
        self.function = function
        self.name = name

    def search(self, entry: Any) -> list[Candidate]:
        return self.function(entry)

    def lookup_identifier(self, entry: Any) -> list[Candidate]:
        return self.function(entry)

    def search_title(self, entry: Any) -> list[Candidate]:
        return self.function(entry)


def default_providers(
    *,
    timeout: float = 10.0,
    max_results: int = 5,
    email: str | None = None,
) -> list[Provider]:
    providers: list[Provider] = [
        DirectURLProvider(timeout=timeout, max_results=max_results),
        ArxivProvider(timeout=timeout, max_results=max_results),
        DataCiteProvider(timeout=timeout, max_results=max_results),
        OpenAlexProvider(timeout=timeout, max_results=max_results, email=email),
        CrossrefProvider(timeout=timeout, max_results=max_results, email=email),
        DBLPProvider(timeout=timeout, max_results=max_results),
        ICLRProceedingsProvider(timeout=timeout, max_results=max_results),
        NeurIPSProceedingsProvider(timeout=timeout, max_results=max_results),
        ICMLProceedingsProvider(timeout=timeout, max_results=max_results),
        ACLProceedingsProvider(timeout=timeout, max_results=max_results),
        EMNLPProceedingsProvider(timeout=timeout, max_results=max_results),
        CVPRProceedingsProvider(timeout=timeout, max_results=max_results),
        ICCVProceedingsProvider(timeout=timeout, max_results=max_results),
        ECCVProceedingsProvider(timeout=timeout, max_results=max_results),
        ACLAnthologyProvider(timeout=timeout, max_results=max_results),
        JMLRProvider(timeout=timeout, max_results=max_results),
        OpenReviewProvider(timeout=timeout, max_results=max_results),
        GitHubProvider(timeout=timeout, max_results=max_results),
    ]
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY"):
        providers.insert(
            5,
            SemanticScholarProvider(timeout=timeout, max_results=max_results),
        )
    return providers


def get_default_providers(**kwargs: Any) -> list[Provider]:
    return default_providers(**kwargs)


def _entry_title(entry: Any) -> str:
    return str(_value(entry, "title") or "")


def _entry_doi(entry: Any) -> str:
    return str(_value(entry, "doi") or "")


def _entry_arxiv(entry: Any) -> str:
    return str(_value(entry, "arxiv_id", "arxiv", "eprint") or "")


def _entry_url(entry: Any) -> str:
    for name in ("url", "howpublished", "note"):
        value = str(_value(entry, name) or "")
        match = re.search(r"https?://[^\s{}<>]+", value, re.IGNORECASE)
        if match:
            return match.group().rstrip(".,;:)]")
    return ""


def _entry_first_author(entry: Any) -> str:
    authors = _value(entry, "authors", "author") or []
    if isinstance(authors, str):
        authors = re.split(r"\s+and\s+", authors, maxsplit=1, flags=re.I)
    if not authors:
        return ""
    author = str(authors[0]).strip()
    if "," in author:
        return author.split(",", 1)[0].strip()
    return author.split()[-1] if author.split() else ""


def _entry_field_values(entry: Any) -> list[str]:
    if isinstance(entry, Mapping):
        return [str(value) for value in entry.values()]
    fields = getattr(entry, "fields", None)
    if isinstance(fields, Mapping):
        return [str(value) for value in fields.values()]
    return [
        str(value)
        for name in ("url", "howpublished", "note", "repository")
        if (value := getattr(entry, name, None))
    ]


def _venue_text(entry: Any) -> str:
    return " ".join(
        str(value)
        for name in ("booktitle", "journal", "venue", "url")
        if (value := _value(entry, name))
    ).casefold()


def _venue_matches(entry: Any, markers: Sequence[str]) -> bool:
    venue = _title_key(_venue_text(entry))
    return any(
        re.search(rf"\b{re.escape(_title_key(marker))}\b", venue)
        for marker in markers
    )


def _anthology_year_volume_ids(
    page: str,
    venue_id: str,
    year: int,
) -> list[str]:
    event_pattern = re.compile(
        rf"href=[\"']?/events/{re.escape(venue_id)}-(?P<year>\d{{4}})/?"
        rf"[\"']?[^>]*>\s*(?P=year)\s*</a>",
        re.IGNORECASE,
    )
    events = list(event_pattern.finditer(page))
    for index, event in enumerate(events):
        if int(event.group("year")) != year:
            continue
        end = events[index + 1].start() if index + 1 < len(events) else len(page)
        section = page[event.end() : end]
        return list(
            dict.fromkeys(
                re.findall(
                    r"href=[\"']?/volumes/([^/\"' >]+)/?",
                    section,
                    flags=re.IGNORECASE,
                )
            )
        )
    return []


def _has_github_reference(entry: Any) -> bool:
    return any("github" in value.casefold() for value in _entry_field_values(entry))


def _github_repository(entry: Any) -> tuple[str, str] | None:
    pattern = re.compile(
        r"(?:https?://)?(?:www\.)?github\.com/"
        r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    )
    excluded = {
        "features",
        "login",
        "marketplace",
        "orgs",
        "search",
        "settings",
        "topics",
        "users",
    }
    for value in _entry_field_values(entry):
        match = pattern.search(value)
        if not match:
            continue
        owner, repository = match.groups()
        repository = repository.rstrip(".,;:)]}").removesuffix(".git")
        if owner.casefold() not in excluded and repository:
            return owner, repository
    return None


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


def _normalize_doi(value: str) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I).casefold()


def _normalize_arxiv(value: str) -> str:
    value = re.sub(r"^.*?arxiv(?:\.org/(?:abs|pdf)/|:)", "", value.strip(), flags=re.I)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    return re.sub(r"v\d+$", "", value, flags=re.I).casefold()


def _extract_doi(*values: Any) -> str:
    for value in values:
        match = DOI_RE.search(str(value or ""))
        if match:
            return match.group().rstrip(".,;)").casefold()
    return ""


def _extract_arxiv(*values: Any) -> str:
    for value in values:
        match = ARXIV_RE.search(str(value or ""))
        if match:
            return match.group(1).casefold()
    return ""


def _extract_arxiv_version(value: Any) -> int | None:
    match = re.search(
        r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})v(\d+)",
        str(value or ""),
        flags=re.I,
    )
    return int(match.group(1)) if match else None


def _year(value: Any) -> int | None:
    match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _crossref_year(item: Mapping[str, Any]) -> int | None:
    for name in ("published-print", "published-online", "published", "issued"):
        parts = (item.get(name) or {}).get("date-parts") or []
        if parts and parts[0]:
            return _year(parts[0][0])
    return None


def _element_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _openreview_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, Mapping) else value


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", unescape(value))).strip()


def _html_metadata(text: str) -> dict[str, list[str]]:
    metadata: dict[str, list[str]] = {}
    for tag in re.findall(r"<meta\b[^>]*>", text, flags=re.IGNORECASE):
        attributes = {
            name.casefold(): unescape(value)
            for name, _, value in re.findall(
                r"([:\w-]+)\s*=\s*([\"'])(.*?)\2",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
        }
        name = str(attributes.get("name") or attributes.get("property") or "")
        content = str(attributes.get("content") or "").strip()
        if name and content:
            metadata.setdefault(name.casefold(), []).append(content)
    return metadata


def _first_metadata(
    metadata: Mapping[str, Sequence[str]], *names: str
) -> str:
    for name in names:
        values = metadata.get(name.casefold()) or []
        if values:
            return str(values[0])
    return ""


def _notion_authors(text: str) -> list[str]:
    body = re.search(
        r"<body\b[^>]*>(.*?)</body>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not body:
        return []
    first_paragraph = re.search(
        r"<p\b[^>]*>(.*?)</p>",
        body.group(1),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not first_paragraph:
        return []
    value = first_paragraph.group(1)
    linked = [
        _strip_html(author)
        for author in re.findall(
            r"<a\b[^>]*>(.*?)</a>",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if _strip_html(author)
    ]
    plain = _strip_html(value)
    if linked and len(linked) >= plain.count(",") + 1:
        return linked
    plain = re.sub(r"\$\^.*?\$", "", plain)
    return [
        re.sub(r"\*+$", "", author).strip()
        for author in plain.split(",")
        if re.sub(r"\*+$", "", author).strip()
    ]


def _notion_date(text: str) -> str:
    body = re.search(
        r"<body\b[^>]*>(.*?)</body>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not body:
        return ""
    match = re.search(
        r"(?:—|&mdash;|-)\s*"
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"[a-z]*\s+\d{1,2},\s+\d{4})",
        body.group(1),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _split_display_authors(value: str) -> list[str]:
    return [
        author.strip()
        for author in re.split(r"\s*,\s*|\s+and\s+", value)
        if author.strip()
    ]


def _sort_title_candidates(title: str, candidates: Sequence[Candidate]) -> list[Candidate]:
    expected = _title_key(title)
    return sorted(
        candidates,
        key=lambda candidate: (
            _title_key(candidate.title) == expected,
            _title_overlap(expected, _title_key(candidate.title)),
        ),
        reverse=True,
    )


def _has_close_title(title: str, candidates: Sequence[Candidate]) -> bool:
    expected = _title_key(title)
    return any(
        _title_key(candidate.title) == expected
        or _title_overlap(expected, _title_key(candidate.title)) >= 0.90
        for candidate in candidates
    )


def _sort_entry_candidates(
    entry: Any,
    candidates: Sequence[Candidate],
) -> list[Candidate]:
    expected_title = _title_key(_entry_title(entry))
    expected_venue = _venue_key(_venue_text(entry))
    return sorted(
        candidates,
        key=lambda candidate: (
            _title_key(candidate.title) == expected_title,
            bool(expected_venue and _venue_key(candidate.venue) == expected_venue),
            _title_overlap(expected_title, _title_key(candidate.title)),
        ),
        reverse=True,
    )


def _venue_key(value: str) -> str:
    normalized = _title_key(value)
    aliases = (
        ("colm", ("colm", "conference on language modeling")),
        ("iclr", ("iclr", "international conference on learning representations")),
        ("icml", ("icml", "international conference on machine learning")),
        ("neurips", ("neurips", "nips", "neural information processing systems")),
        ("acl", ("acl", "annual meeting of the association for computational linguistics")),
        ("emnlp", ("emnlp", "empirical methods in natural language processing")),
        ("cvpr", ("cvpr", "computer vision and pattern recognition")),
        ("iccv", ("iccv", "international conference on computer vision")),
        ("eccv", ("eccv", "european conference on computer vision")),
        ("tacl", ("tacl", "transactions of the association for computational linguistics")),
        ("jmlr", ("jmlr", "journal of machine learning research")),
    )
    for canonical, markers in aliases:
        if any(
            re.search(rf"\b{re.escape(marker)}\b", normalized)
            for marker in markers
        ):
            return canonical
    return normalized


def _title_key(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _title_overlap(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens)
    return 2 * overlap / (len(left_tokens) + len(right_tokens))


def _timestamp_year(value: Any) -> int | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).year
    except (OverflowError, OSError, ValueError):
        return None


def _lookup_title(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _lookup_keys(entry: Any) -> list[str]:
    keys: list[str] = []
    doi, arxiv_id, title = _entry_doi(entry), _entry_arxiv(entry), _entry_title(entry)
    if doi:
        keys.append(f"doi:{_normalize_doi(doi)}")
    if arxiv_id:
        keys.append(f"arxiv:{_normalize_arxiv(arxiv_id)}")
    if title:
        keys.extend((f"title:{title}", f"title:{_lookup_title(title)}"))
    return keys


def _record_matches(entry: Any, record: Mapping[str, Any]) -> bool:
    expected_doi, expected_arxiv = _entry_doi(entry), _entry_arxiv(entry)
    if expected_doi and _normalize_doi(expected_doi) == _extract_doi(record.get("doi"), record.get("url")):
        return True
    if expected_arxiv and _normalize_arxiv(expected_arxiv) == _extract_arxiv(
        record.get("arxiv_id"),
        record.get("arxiv"),
        record.get("eprint"),
        record.get("url"),
    ):
        return True
    return bool(_entry_title(entry) and _lookup_title(_entry_title(entry)) == _lookup_title(str(record.get("title") or "")))


def _fixture_records(fixture: Any) -> list[Mapping[str, Any]]:
    if isinstance(fixture, Mapping):
        if "records" in fixture:
            return _as_records(fixture["records"])
        records: list[Mapping[str, Any]] = []
        for value in fixture.values():
            records.extend(_as_records(value))
        return records
    return _as_records(fixture)


def _as_records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _dedupe(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for record in records:
        if id(record) not in seen:
            seen.add(id(record))
            result.append(record)
    return result


__all__ = [
    "ArXivProvider",
    "ArxivProvider",
    "DataCiteProvider",
    "CrossrefProvider",
    "DBLPProvider",
    "DirectURLProvider",
    "ICLRProceedingsProvider",
    "NeurIPSProceedingsProvider",
    "ICMLProceedingsProvider",
    "ACLProceedingsProvider",
    "EMNLPProceedingsProvider",
    "CVPRProceedingsProvider",
    "ICCVProceedingsProvider",
    "ECCVProceedingsProvider",
    "ACLAnthologyProvider",
    "JMLRProvider",
    "FixtureProvider",
    "FunctionProvider",
    "GitHubProvider",
    "LocalProvider",
    "OpenAlexProvider",
    "OpenReviewProvider",
    "Provider",
    "default_providers",
    "get_default_providers",
]
