from dataclasses import dataclass, field
import re
from typing import Any, Optional


@dataclass
class BibEntry:
    key: str
    entry_type: str
    fields: dict[str, str]

    def __post_init__(self) -> None:
        self.key = self.key.strip()
        self.entry_type = self.entry_type.strip().lower()
        self.fields = {str(name).strip().lower(): str(value).strip() for name, value in self.fields.items()}

    @property
    def title(self) -> str:
        return self.fields.get("title", "")

    @property
    def author(self) -> str:
        return self.fields.get("author", "")

    @property
    def authors(self) -> list[str]:
        """按 BibTeX 顶层的 ``and`` 分隔符拆分作者。"""

        value = self.author
        authors: list[str] = []
        start = 0
        depth = 0
        index = 0
        while index < len(value):
            char = value[index]
            if char == "\\":
                index += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}" and depth:
                depth -= 1
            elif (
                depth == 0
                and value[index : index + 3].lower() == "and"
                and (index == 0 or value[index - 1].isspace())
                and (index + 3 == len(value) or value[index + 3].isspace())
            ):
                author = value[start:index].strip()
                if author:
                    authors.append(author)
                start = index + 3
                index += 3
                continue
            index += 1
        author = value[start:].strip()
        if author:
            authors.append(author)
        return authors

    @property
    def year(self) -> Optional[int]:
        value = self.fields.get("year", "")
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits[:4]) if len(digits) >= 4 else None

    @property
    def doi(self) -> str:
        value = self.fields.get("doi", "").strip()
        return re.sub(
            r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)",
            "",
            value,
            flags=re.IGNORECASE,
        ).rstrip(".")

    @property
    def arxiv_id(self) -> str:
        identifier_re = re.compile(
            r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v\d+)?", re.IGNORECASE,
        )
        for field in (
            "eprint",
            "arxiv",
            "arxivid",
            "url",
            "doi",
            "journal",
            "howpublished",
            "note",
        ):
            value = self.fields.get(field, "").strip()
            if not value:
                continue
            value = re.sub(
                r"^https?://arxiv\.org/(?:abs|pdf)/",
                "",
                value,
                flags=re.IGNORECASE,
            )
            value = re.sub(r"^arxiv\s*:\s*", "", value, flags=re.IGNORECASE)
            value = value.split("?", 1)[0].removesuffix(".pdf")
            match = identifier_re.search(value)
            if match:
                return match.group(0)
        return ""


@dataclass
class Candidate:
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    url: str = ""
    identifier: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    key: str
    status: str
    reasons: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "reasons": self.reasons,
            "candidates": [
                {
                    "source": item.source,
                    "title": item.title,
                    "authors": item.authors,
                    "year": item.year,
                    "venue": item.venue,
                    "url": item.url,
                    "identifier": item.identifier,
                }
                for item in self.candidates
            ],
        }
