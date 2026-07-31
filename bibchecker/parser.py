"""不依赖第三方库的 BibTeX 与 LaTeX 解析工具。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
import re

from .models import BibEntry


_IGNORED_BIB_BLOCKS = {"comment", "preamble", "string"}
_COMMAND_RE = re.compile(r"\\([A-Za-z@]+)")
_VERBATIM_ENV_RE = re.compile(
    r"\\begin\s*\{(?P<env>verbatim\*?|Verbatim|lstlisting|minted|comment)\}"
    r".*?\\end\s*\{(?P=env)\}",
    re.DOTALL,
)
_CITATION_COMMANDS = {
    "autocite",
    "autocites",
    "cite",
    "citealp",
    "citealt",
    "citeauthor",
    "citedate",
    "citefield",
    "citefullauthor",
    "citelabel",
    "citep",
    "cites",
    "citet",
    "citetext",
    "citetitle",
    "citeurl",
    "citeyear",
    "citeyearpar",
    "footcite",
    "footcites",
    "footfullcite",
    "fullcite",
    "headlesscite",
    "nocite",
    "notecite",
    "onlinecite",
    "parencite",
    "parencites",
    "smartcite",
    "smartcites",
    "supercite",
    "textcite",
    "textcites",
}
_MULTI_CITATION_COMMANDS = {
    command for command in _CITATION_COMMANDS if command.endswith("cites")
}


def parse_bib(path: str | Path) -> dict[str, BibEntry]:
    """将 BibTeX 文件解析为以引用键为键的映射。

    支持大括号值和引号值、嵌套大括号、``#`` 拼接、``@string`` 宏，以及
    ``{...}`` 和 ``(...)`` 两种条目定界格式。解析时会跳过 ``@comment``、
    ``@preamble`` 等特殊块。
    """

    text = Path(path).read_text(encoding="utf-8-sig")
    blocks = list(_iter_bib_blocks(_strip_percent_comments(text)))

    raw_macros: dict[str, str] = {}
    for entry_type, content in blocks:
        if entry_type != "string":
            continue
        for assignment in _split_top_level(content, ","):
            name, expression = _split_assignment(assignment)
            if name is not None:
                raw_macros[name.lower()] = expression

    resolved_macros: dict[str, str] = {}

    def resolve_macro(name: str, resolving: set[str] | None = None) -> str | None:
        normalized = name.lower()
        if normalized in resolved_macros:
            return resolved_macros[normalized]
        if normalized not in raw_macros:
            return None
        resolving = set() if resolving is None else resolving
        if normalized in resolving:
            return name
        resolving.add(normalized)
        value = _parse_value(raw_macros[normalized], lambda item: resolve_macro(item, resolving))
        resolving.remove(normalized)
        resolved_macros[normalized] = value
        return value

    entries: dict[str, BibEntry] = {}
    for entry_type, content in blocks:
        if entry_type in _IGNORED_BIB_BLOCKS:
            continue
        key, field_text = _split_entry_content(content)
        if not key:
            continue

        fields: dict[str, str] = {}
        for assignment in _split_top_level(field_text, ","):
            name, expression = _split_assignment(assignment)
            if name is not None:
                fields[name.lower()] = _parse_value(expression, resolve_macro)
        entries[key] = BibEntry(key=key, entry_type=entry_type, fields=fields)
    return entries


def find_citation_keys(tex_path: str | Path) -> set[str]:
    """查找 LaTeX 文件及其递归引入文件中的引用键。

    ``tex_path`` 可以指向一个根 ``.tex`` 文件，也可以指向一个目录；如果传入
    目录，则将其中的所有 ``.tex`` 文件作为根文件。
    """

    requested_path = Path(tex_path)
    citations: set[str] = set()
    visited: set[Path] = set()

    def visit(path: Path, *, required: bool = False) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        if not path.is_file():
            if required:
                path.read_text(encoding="utf-8-sig")
            return

        visited.add(resolved)
        file_citations, includes = _scan_latex(path.read_text(encoding="utf-8-sig"))
        citations.update(file_citations)
        for include in includes:
            visit(_resolve_tex_path(path.parent / include))

    if requested_path.is_dir():
        for root in sorted(requested_path.rglob("*.tex")):
            visit(root)
    else:
        visit(_resolve_tex_path(requested_path), required=True)
    return citations


def find_uncited_keys(entries: Mapping[str, BibEntry], citation_keys: Iterable[str]) -> set[str]:
    """返回不在给定引用键集合中的参考文献键。"""

    cited = set(citation_keys)
    if "*" in cited:
        return set()
    return set(entries).difference(cited)


def find_uncited_entries(bib_path: str | Path, tex_path: str | Path) -> dict[str, BibEntry]:
    """解析一组 BibTeX/LaTeX 文件，并返回未被引用的条目。"""

    entries = parse_bib(bib_path)
    unused = find_uncited_keys(entries, find_citation_keys(tex_path))
    return {key: entry for key, entry in entries.items() if key in unused}


def _strip_percent_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "%" and not _is_escaped(text, index):
            newline = text.find("\n", index)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        output.append(text[index])
        index += 1
    return "".join(output)


def _iter_bib_blocks(text: str) -> Iterator[tuple[str, str]]:
    index = 0
    while index < len(text):
        marker = text.find("@", index)
        if marker < 0:
            return
        name_start = marker + 1
        while name_start < len(text) and text[name_start].isspace():
            name_start += 1
        name_end = name_start
        while name_end < len(text) and (text[name_end].isalnum() or text[name_end] in "_-"):
            name_end += 1
        entry_type = text[name_start:name_end].lower()
        opener_index = name_end
        while opener_index < len(text) and text[opener_index].isspace():
            opener_index += 1
        if not entry_type or opener_index >= len(text):
            index = marker + 1
            continue

        opener = text[opener_index]
        if opener not in "{(":
            index = marker + 1
            continue
        closer = "}" if opener == "{" else ")"
        end = _find_bib_block_end(text, opener_index, opener, closer)
        if end is None:
            return
        yield entry_type, text[opener_index + 1 : end]
        index = end + 1


def _find_bib_block_end(
    text: str,
    start: int,
    opener: str,
    closer: str,
) -> int | None:
    outer_depth = 1
    brace_depth = 0
    quoted = False
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            quoted = not quoted
            index += 1
            continue
        if quoted:
            index += 1
            continue

        if opener == "{":
            if char == "{":
                outer_depth += 1
            elif char == "}":
                outer_depth -= 1
                if outer_depth == 0:
                    return index
        else:
            if char == "{":
                brace_depth += 1
            elif char == "}" and brace_depth:
                brace_depth -= 1
            elif brace_depth == 0:
                if char == "(":
                    outer_depth += 1
                elif char == ")":
                    outer_depth -= 1
                    if outer_depth == 0:
                        return index
        index += 1
    return None


def _split_entry_content(content: str) -> tuple[str, str]:
    parts = _split_top_level(content, ",", maxsplit=1)
    return (
        parts[0].strip() if parts else "", parts[1] if len(parts) == 2 else "",
    )


def _split_assignment(assignment: str) -> tuple[str | None, str]:
    parts = _split_top_level(assignment, "=", maxsplit=1)
    if len(parts) != 2 or not parts[0].strip():
        return None, ""
    return parts[0].strip(), parts[1].strip()


def _split_top_level(
    text: str,
    separator: str,
    *,
    maxsplit: int = -1,
) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quoted = False
    splits = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "{":
                depth += 1
            elif char == "}" and depth:
                depth -= 1
            elif (
                char == separator
                and depth == 0
                and (maxsplit < 0 or splits < maxsplit)
            ):
                parts.append(text[start:index])
                start = index + 1
                splits += 1
        index += 1
    parts.append(text[start:])
    return parts


def _parse_value(
    expression: str,
    resolve_macro: Callable[[str], str | None],
) -> str:
    values: list[str] = []
    for part in _split_top_level(expression.strip(), "#"):
        value = part.strip()
        if not value:
            continue
        if _has_outer_delimiters(value, "{", "}"):
            values.append(value[1:-1])
        elif _has_outer_delimiters(value, '"', '"'):
            values.append(value[1:-1])
        else:
            values.append(resolve_macro(value) or value)
    return re.sub(r"\s+", " ", "".join(values)).strip()


def _has_outer_delimiters(value: str, opener: str, closer: str) -> bool:
    if len(value) < 2 or value[0] != opener or value[-1] != closer:
        return False
    if opener == '"':
        return not _is_escaped(value, len(value) - 1)

    depth = 0
    for index, char in enumerate(value):
        if char == "\\":
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return False
    return depth == 0


def _scan_latex(text: str) -> tuple[set[str], list[str]]:
    text = _mask_verbatim(_strip_percent_comments(text))
    citations: set[str] = set()
    includes: list[str] = []
    for match in _COMMAND_RE.finditer(text):
        if _is_escaped(text, match.start()):
            continue
        command = match.group(1).lower()
        if command in _CITATION_COMMANDS:
            arguments = _citation_arguments(
                text, match.end(), multiple=command in _MULTI_CITATION_COMMANDS,
            )
            for argument in arguments:
                citations.update(
                    key for key in (item.strip() for item in argument.split(",")) if key
                )
        elif command in {"input", "include"}:
            argument = _include_argument(text, match.end())
            if argument:
                includes.extend(item.strip() for item in argument.split(",") if item.strip())
    return citations, includes


def _citation_arguments(
    text: str,
    position: int,
    *,
    multiple: bool,
) -> list[str]:
    position = _skip_space(text, position)
    if position < len(text) and text[position] == "*":
        position = _skip_space(text, position + 1)

    arguments: list[str] = []
    while True:
        while position < len(text) and text[position] == "[":
            group = _read_group(text, position, "[", "]")
            if group is None:
                return arguments
            _, position = group
            position = _skip_space(text, position)
        if position >= len(text) or text[position] != "{":
            return arguments
        group = _read_group(text, position, "{", "}")
        if group is None:
            return arguments
        argument, position = group
        arguments.append(argument)
        if not multiple:
            return arguments
        position = _skip_space(text, position)


def _include_argument(text: str, position: int) -> str | None:
    position = _skip_space(text, position)
    if position >= len(text):
        return None
    if text[position] == "{":
        group = _read_group(text, position, "{", "}")
        return group[0] if group is not None else None
    end = position
    while end < len(text) and not text[end].isspace() and text[end] not in "{}":
        end += 1
    return text[position:end] or None


def _read_group(
    text: str,
    position: int,
    opener: str,
    closer: str,
) -> tuple[str, int] | None:
    if position >= len(text) or text[position] != opener:
        return None
    depth = 1
    index = position + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[position + 1 : index], index + 1
        index += 1
    return None


def _skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _mask_verbatim(text: str) -> str:
    text = _VERBATIM_ENV_RE.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    chars = list(text)
    for match in re.finditer(r"\\verb\*?", text):
        delimiter_index = match.end()
        if delimiter_index >= len(text):
            continue
        delimiter = text[delimiter_index]
        if delimiter.isspace() or delimiter.isalnum():
            continue
        end = text.find(delimiter, delimiter_index + 1)
        if end < 0:
            continue
        for index in range(match.start(), end + 1):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _resolve_tex_path(path: Path) -> Path:
    if path.is_file():
        return path
    with_tex_suffix = Path(f"{path}.tex")
    return with_tex_suffix if with_tex_suffix.is_file() else path


def _is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


__all__ = [
    "BibEntry",
    "find_citation_keys",
    "find_uncited_entries",
    "find_uncited_keys",
    "parse_bib",
]
