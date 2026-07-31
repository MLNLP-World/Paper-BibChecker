import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping, Iterator, TextIO

from .checker import CheckResult, check_entry
from .models import BibEntry
from .parser import find_citation_keys, parse_bib
from .providers import default_providers


STATUS_LABELS = {
    "validated": "✅ 通过",
    "needs_review": "⚠️ 信息需核对",
    "likely_hallucination": "❌ 疑似幻觉",
    "unconfirmed": "❓ 无法确认",
}

FIELD_LABELS = {
    "title": "标题",
    "authors": "作者",
    "year": "年份",
    "venue": "会议或期刊",
    "doi": "DOI",
    "arxiv_id": "arXiv ID",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="检查 Bib 引用是否对应真实文献。",
        add_help=False,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")
    parser.add_argument("bib", help="Bib 文件")
    parser.add_argument(
        "--tex", help="TeX 文件；只检查可从该文件访问到的引用"
    )
    parser.add_argument(
        "--check-unused",
        action="store_true",
        help="同时检查 TeX 中未引用的 Bib 条目",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 格式输出完整结果",
    )
    parser.add_argument(
        "--log-file",
        help="日志文件；默认为与 Bib 同名的 .log 文件",
    )
    parser.add_argument(
        "--issues-file",
        help="问题引用 JSON；默认为 <文件名>_issues.json",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="HTTP 超时时间（秒）"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="每批并行检查的引用数量",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size 必须至少为 1")

    log_path, issues_path = _output_paths(
        args.bib, args.log_file, args.issues_file
    )
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with log_path.open("w", encoding="utf-8") as log:
        sys.stdout = _Tee(original_stdout, log)
        sys.stderr = _Tee(original_stderr, log)
        try:
            _run(args, log_path, issues_path)
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


def _run(args: argparse.Namespace, log_path: Path, issues_path: Path) -> None:
    entries = parse_bib(args.bib)
    cited = find_citation_keys(args.tex) if args.tex else set(entries)
    missing = sorted(cited.difference(entries))
    keys = sorted(set(entries) if args.check_unused else cited.intersection(entries))
    providers = default_providers(timeout=args.timeout)
    results = []
    progress = sys.stderr if args.as_json else sys.stdout
    allocated_elapsed = 0.0
    printed_items = 0

    for (
        start,
        batch_keys,
        batch_results,
        batch_elapsed,
    ) in _check_batches(entries, keys, providers, args.batch_size):
        batch_base_elapsed = allocated_elapsed
        for offset, (key, result) in enumerate(
            zip(batch_keys, batch_results), start
        ):
            results.append(result)
            item_in_batch = offset - start + 1
            elapsed, average = _allocated_timing(
                batch_base_elapsed,
                batch_elapsed,
                item_in_batch,
                len(batch_keys),
                offset,
            )
            progress_line = (
                f"[{offset}/{len(keys)}] {key}  | "
                f"已用 {_format_duration(elapsed)}  | "
                f"平均每条 {_format_duration(average)}"
            )
            if args.as_json:
                print(progress_line, file=progress, flush=True)
            else:
                if printed_items:
                    print()
                print(progress_line, flush=True)
                _print_result(result)
                printed_items += 1
        allocated_elapsed += batch_elapsed

    if args.as_json:
        _write_issues_file(issues_path, args.bib, results, missing)
        print(
            json.dumps(
                {
                    "missing_citation_keys": missing,
                    "results": [item.as_dict() for item in results],
                    "log_file": str(log_path),
                    "issues_file": str(issues_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    for key in missing:
        print(f"\n[缺少条目] {key}\n  TeX 使用了该引用键，但 Bib 中不存在。")

    counts: dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = "  ".join(
        f"{STATUS_LABELS.get(status, status)} {count}"
        for status, count in sorted(counts.items())
    )
    print(f"\n检查完成：共 {len(results)} 条  {summary}")
    _print_final_summary(results, entries)
    _write_issues_file(issues_path, args.bib, results, missing)
    print(f"\n日志文件：{log_path}")
    print(f"问题引用文件：{issues_path}")


class _Tee:
    def __init__(self, stream: TextIO, log: TextIO) -> None:
        self.stream = stream
        self.log = log

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.log.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.log.flush()


def _output_paths(
    bib: str,
    log_file: str | None,
    issues_file: str | None,
) -> tuple[Path, Path]:
    bib_path = Path(bib)
    return (
        Path(log_file) if log_file else bib_path.with_suffix(".log"),
        (
            Path(issues_file)
            if issues_file
            else bib_path.with_name(f"{bib_path.stem}_issues.json")
        ),
    )


def _write_issues_file(
    path: Path,
    bib: str,
    results: Iterable[CheckResult],
    missing: Iterable[str],
) -> None:
    issues = []
    for result in results:
        if result.status == "validated":
            continue
        issues.append(
            {
                "key": result.key,
                "problems": _issue_problems(result),
            }
        )
    path.write_text(
        json.dumps(
            {
                "bib": str(bib),
                "issue_count": len(issues),
                "missing_citation_keys": list(missing),
                "issues": issues,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _issue_problems(result: CheckResult) -> list[str]:
    differences = _format_differences(result.field_comparison)
    if differences:
        problems = result.reasons[:1] + differences
    else:
        problems = list(result.reasons)
    return problems or ["未提供具体问题"]


def _print_result(result: CheckResult) -> None:
    label = STATUS_LABELS.get(result.status, result.status)
    print(f"[{label}]")
    if result.reasons:
        print(f"  结论：{result.reasons[0]}")

    differences = _format_differences(result.field_comparison)
    if differences:
        print("  差异：")
        for difference in differences:
            print(f"    • {difference}")

    best = result.best_candidate
    if best and result.status != "validated" and (
        best.identifier_match
        or best.title_score >= 0.72
        or result.status == "needs_review"
    ):
        source = best.source
        if best.url:
            print(f"  来源：{source} · {best.url}")
        else:
            print(f"  来源：{source}")

    if result.provider_errors:
        providers = ", ".join(
            sorted({name.split(":", 1)[0] for name in result.provider_errors})
        )
        print(f"  未完成的数据源：{providers}（检索失败/超时，未计入上述数量）")


def _print_final_summary(
    results: Iterable[CheckResult],
    entries: Mapping[str, BibEntry],
) -> None:
    results = list(results)
    grouped = {
        status: [result for result in results if result.status == status]
        for status in ("likely_hallucination", "needs_review", "unconfirmed")
    }
    validated_count = sum(result.status == "validated" for result in results)
    print("\n最终汇总：")
    for status, items in grouped.items():
        print(f"{STATUS_LABELS[status]}（{len(items)}）")
        if not items:
            print("  无")
            continue
        for index, result in enumerate(items, 1):
            entry = entries.get(result.key)
            title = getattr(entry, "title", "") or "未提供标题"
            authors = getattr(entry, "authors", []) or []
            author_text = "；".join(str(author) for author in authors) or "未提供作者"
            print(f"  {index}. 标题：{title}")
            print(f"     作者：{author_text}")
            print("     原因：")
            for reason in result.reasons or ["未提供原因"]:
                print(f"       - {reason}")
    print(f"{STATUS_LABELS['validated']}（{validated_count}，仅统计数量）")


def _check_batches(
    entries: Mapping[str, BibEntry],
    keys: list[str],
    providers: Iterable[Any],
    batch_size: int,
) -> Iterator[tuple[int, list[str], list[CheckResult]]]:
    for start_index in range(0, len(keys), batch_size):
        batch_keys = keys[start_index : start_index + batch_size]
        batch_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = [
                executor.submit(check_entry, entries[key], providers)
                for key in batch_keys
            ]
            batch_results = [future.result() for future in futures]
        yield (
            start_index + 1,
            batch_keys,
            batch_results,
            time.perf_counter() - batch_start,
        )


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _allocated_timing(
    previous_elapsed: float,
    batch_elapsed: float,
    item_in_batch: int,
    batch_count: int,
    completed_items: int,
) -> tuple[float, float]:
    elapsed = previous_elapsed + batch_elapsed * item_in_batch / batch_count
    return elapsed, elapsed / completed_items


def _format_differences(
    comparison: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    output: list[str] = []
    for field, item in comparison.items():
        status = item.get("status")
        if field == "authors" and status in {
            "minor_difference",
            "major_mismatch",
        }:
            if item.get("added"):
                output.append("作者新增：" + "；".join(item["added"]))
            if item.get("removed"):
                output.append("作者删除：" + "；".join(item["removed"]))
            if item.get("reordered"):
                output.append("作者顺序：" + "；".join(item["reordered"]))
            if status == "major_mismatch" and not any(
                item.get(name) for name in ("added", "removed", "reordered")
            ):
                output.append("大部分作者无法对应")
        elif status == "mismatch":
            label = FIELD_LABELS.get(field, field)
            output.append(
                f"{label}：Bib={item.get('bibtex')!r}；"
                f"检索={item.get('retrieved')!r}"
            )
    return output


if __name__ == "__main__":
    main()
