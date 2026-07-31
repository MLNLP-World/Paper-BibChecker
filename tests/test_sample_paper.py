from bibchecker.checker import LIKELY_HALLUCINATION, check_entries
from bibchecker.models import BibEntry
from bibchecker.parser import find_citation_keys, parse_bib
from bibchecker.providers import LocalProvider


def _write_synthetic_paper(tmp_path):
    bib = tmp_path / "synthetic_references.bib"
    tex = tmp_path / "synthetic_paper.tex"
    bib.write_text(
        r"""
        @article{synthetic2024alpha,
          title={A Reliable Benchmark for Evaluating Scientific Citation Metadata},
          author={Smith, Alice and Jones, Bob},
          year={2024},
        }
        @article{synthetic2024beta,
          title={A Practical Framework for Detecting Inconsistent Bibliographic Records},
          author={Wang, Dan and Lee, Carol},
          year={2024},
        }
        @article{synthetic2024gamma,
          title={A Reproducible Study of Metadata Quality in Research Workflows},
          author={Brown, Eve and Taylor, Frank},
          year={2024},
        }
        @article{synthetic2024unused,
          title={An Unused Reference Kept for Coverage Testing},
          author={Miller, Grace},
          year={2024},
        }
        """,
        encoding="utf-8",
    )
    tex.write_text(
        r"""
        \documentclass{article}
        \begin{document}
        \cite{synthetic2024alpha,synthetic2024beta}
        \cite{synthetic2024gamma}
        \end{document}
        """,
        encoding="utf-8",
    )
    return bib, tex


def test_synthetic_paper_parser_and_citation_selection(tmp_path):
    bib, tex = _write_synthetic_paper(tmp_path)

    entries = parse_bib(bib)
    citations = find_citation_keys(tex)

    assert len(entries) == 4
    assert citations == {
        "synthetic2024alpha",
        "synthetic2024beta",
        "synthetic2024gamma",
    }
    assert citations <= entries.keys()


def test_synthetic_high_risk_entries_are_detected_with_empty_sources():
    entries = {
        "synthetic_missing_alpha": BibEntry(
            "synthetic_missing_alpha",
            "article",
            {
                "title": (
                    "A Highly Specific Method for Verifying "
                    "Unpublished Citation Metadata"
                ),
                "author": (
                    "Smith, Alice and Jones, Bob and Taylor, Carol"
                ),
                "year": "2024",
            },
        ),
        "synthetic_missing_beta": BibEntry(
            "synthetic_missing_beta",
            "article",
            {
                "title": (
                    "A Distinctive Benchmark for Auditing "
                    "Inconsistent Research References"
                ),
                "author": "Wang, Dan and Lee, Carol and Brown, Eve",
                "year": "2024",
            },
        ),
    }
    providers = [
        LocalProvider([], name="source-a"),
        LocalProvider([], name="source-b"),
        LocalProvider([], name="source-c"),
    ]

    results = check_entries(entries, sorted(entries), providers)

    assert {result.key for result in results} == set(entries)
    assert all(result.status == LIKELY_HALLUCINATION for result in results)


def test_synthetic_wrong_arxiv_ids_point_to_unrelated_records():
    entries = {
        "synthetic_wrong_alpha": BibEntry(
            "synthetic_wrong_alpha",
            "article",
            {
                "title": "A Correctly Named Citation Record",
                "author": "Smith, Alice",
                "year": "2024",
                "eprint": "2401.12345",
            },
        ),
        "synthetic_wrong_beta": BibEntry(
            "synthetic_wrong_beta",
            "article",
            {
                "title": "Another Correctly Named Citation Record",
                "author": "Wang, Dan",
                "year": "2024",
                "eprint": "2402.23456",
            },
        ),
    }
    fixture = {
        "arxiv:2401.12345": [
            {
                "title": "An Unrelated Record Returned by the Identifier",
                "authors": ["Unrelated Author"],
                "year": 2024,
                "eprint": "2401.12345",
            }
        ],
        "arxiv:2402.23456": [
            {
                "title": "A Different Unrelated Record",
                "authors": ["Another Author"],
                "year": 2024,
                "eprint": "2402.23456",
            }
        ],
    }

    results = check_entries(
        entries,
        sorted(entries),
        LocalProvider(fixture),
    )

    assert all(result.status == LIKELY_HALLUCINATION for result in results)
