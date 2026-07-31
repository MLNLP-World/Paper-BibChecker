import time
import sys
import json

import bibchecker.cli as cli
from bibchecker.checker import CheckResult
from bibchecker.models import BibEntry


def test_check_batches_run_in_parallel_and_keep_input_order(monkeypatch):
    entries = {
        key: BibEntry(key, "article", {"title": key})
        for key in ("first", "second", "third")
    }
    completed = []

    def fake_check(entry, providers):
        delays = {"first": 0.04, "second": 0.01, "third": 0.02}
        time.sleep(delays[entry.key])
        completed.append(entry.key)
        return CheckResult(entry.key, "validated")

    monkeypatch.setattr(cli, "check_entry", fake_check)
    batches = list(
        cli._check_batches(
            entries,
            ["first", "second", "third"],
            [],
            batch_size=2,
        )
    )

    assert completed[:2] == ["second", "first"]
    assert batches[0][1] == ["first", "second"]
    assert [result.key for result in batches[0][2]] == ["first", "second"]
    assert batches[0][3] >= 0
    assert batches[1][1] == ["third"]


def test_duration_format_is_compact():
    assert cli._format_duration(1.234) == "1.23s"


def test_batch_time_is_evenly_allocated_to_items():
    first = cli._allocated_timing(2.0, 8.0, 1, 4, 3)
    last = cli._allocated_timing(2.0, 8.0, 4, 4, 6)

    assert first == (4.0, 4.0 / 3)
    assert last == (10.0, 10.0 / 6)


def test_result_heading_has_emoji_without_repeating_key(capsys):
    cli._print_result(CheckResult("paper-key", "validated", ["字段一致"]))

    output = capsys.readouterr().out
    assert output.startswith("[✅ 通过]\n")
    assert "paper-key" not in output


def test_main_keeps_blank_line_between_batches(monkeypatch, tmp_path, capsys):
    bib = tmp_path / "references.bib"
    bib.write_text(
        "\n".join(
            f"@article{{{key}, title={{{key}}}}}"
            for key in ("first", "second", "third")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "default_providers", lambda **kwargs: [])
    monkeypatch.setattr(
        cli,
        "check_entry",
        lambda entry, providers: CheckResult(
            entry.key, "validated", ["字段一致"]
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["bibchecker", str(bib), "--batch-size", "2"],
    )

    cli.main()

    output = capsys.readouterr().out
    assert "字段一致\n\n[2/3]" in output
    assert "字段一致\n\n[3/3]" in output


def test_provider_error_output_says_it_is_not_counted(capsys):
    cli._print_result(
        CheckResult(
            "paper",
            "likely_hallucination",
            ["4 个已完成的独立学术数据源均未找到可信标题匹配"],
            provider_errors={"dblp:title": "timeout"},
        )
    )

    output = capsys.readouterr().out
    assert "未完成的数据源：dblp（检索失败/超时，未计入上述数量）" in output


def test_final_summary_lists_non_validated_items_only(capsys):
    entries = {
        "bad": BibEntry(
            "bad",
            "article",
            {
                "title": "A Suspicious Paper",
                "author": "Smith, Alice and Jones, Bob",
            },
        ),
        "minor": BibEntry(
            "minor",
            "article",
            {
                "title": "A Paper With a Year Difference",
                "author": "Taylor, Carol",
            },
        ),
        "good": BibEntry(
            "good",
            "article",
            {"title": "A Validated Paper", "author": "Wang, Dan"},
        ),
    }
    cli._print_final_summary(
        [
            CheckResult(
                "bad",
                "likely_hallucination",
                ["没有可信标题匹配", "多个数据源未找到"],
            ),
            CheckResult("minor", "needs_review", ["年份不一致"]),
            CheckResult("good", "validated", ["字段一致"]),
        ],
        entries,
    )

    output = capsys.readouterr().out
    assert "最终汇总：" in output
    assert "❌ 疑似幻觉（1）" in output
    assert "⚠️ 信息需核对（1）" in output
    assert "❓ 无法确认（0）" in output
    assert "✅ 通过（1，仅统计数量）" in output
    assert "标题：A Suspicious Paper" in output
    assert "作者：Smith, Alice；Jones, Bob" in output
    assert "- 没有可信标题匹配" in output
    assert "A Validated Paper" not in output


def test_main_writes_log_and_problematic_references(
    monkeypatch, tmp_path, capsys
):
    bib = tmp_path / "references.bib"
    bib.write_text(
        """
        @article{bad,
          title={A Suspicious Paper},
          author={Smith, Alice and Jones, Bob},
          year={2025},
        }
        @article{good,
          title={A Validated Paper},
          author={Wang, Dan},
          year={2024},
        }
        """,
        encoding="utf-8",
    )
    log = tmp_path / "run.log"
    issues = tmp_path / "issues.json"

    monkeypatch.setattr(cli, "default_providers", lambda **kwargs: [])

    def fake_check(entry, providers):
        if entry.key == "bad":
            return CheckResult(
                "bad",
                "likely_hallucination",
                ["没有可信标题匹配"],
            )
        return CheckResult("good", "validated", ["字段一致"])

    monkeypatch.setattr(cli, "check_entry", fake_check)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bibchecker",
            str(bib),
            "--log-file",
            str(log),
            "--issues-file",
            str(issues),
        ],
    )

    cli.main()

    output = capsys.readouterr().out
    log_text = log.read_text(encoding="utf-8")
    issue_data = json.loads(issues.read_text(encoding="utf-8"))
    assert "检查完成：共 2 条" in output
    assert "检查完成：共 2 条" in log_text
    assert "标题：A Suspicious Paper" in log_text
    assert issue_data["issue_count"] == 1
    assert issue_data["issues"][0]["key"] == "bad"
    assert issue_data["issues"][0]["problems"] == ["没有可信标题匹配"]
    assert set(issue_data["issues"][0]) == {"key", "problems"}
    assert "good" not in {item["key"] for item in issue_data["issues"]}


def test_default_output_paths_use_compact_names(tmp_path):
    bib = tmp_path / "example_ref.bib"

    log, issues = cli._output_paths(str(bib), None, None)

    assert log == tmp_path / "example_ref.log"
    assert issues == tmp_path / "example_ref_issues.json"


def test_issue_problems_keep_readable_differences_without_transient_failures():
    result = CheckResult(
        "paper",
        "needs_review",
        [
            "找到可信论文，但存在字段差异",
            "authors: 新增 Alice 删除 Bob",
        ],
        provider_errors={"crossref:title": "timeout"},
        field_comparison={
            "authors": {
                "status": "minor_difference",
                "added": ["Alice（检索第2位）"],
                "removed": ["Bob（Bib第2位）"],
                "reordered": [],
            }
        },
    )

    assert cli._issue_problems(result) == [
        "找到可信论文，但存在字段差异",
        "作者新增：Alice（检索第2位）",
        "作者删除：Bob（Bib第2位）",
    ]
