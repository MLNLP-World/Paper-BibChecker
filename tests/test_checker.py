import time

from bibchecker.checker import (
    CONFLICT,
    LIKELY_HALLUCINATION,
    NEEDS_REVIEW,
    UNCONFIRMED,
    VALIDATED,
    VERIFIED,
    check_entry,
)
from bibchecker.models import BibEntry, Candidate
from bibchecker.providers import FunctionProvider, LocalProvider


def test_identifier_match_is_validated():
    entry = BibEntry(
        "real",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice and Jones, Bob",
            "year": "2024",
            "eprint": "2401.12345",
        },
    )
    provider = LocalProvider(
        [
            {
                "title": "A Real Paper",
                "authors": ["Alice Smith", "Bob Jones"],
                "year": 2024,
                "eprint": "2401.12345",
            }
        ]
    )
    assert check_entry(entry, provider).status == VALIDATED


def test_wrong_arxiv_identifier_is_reported_as_conflict():
    entry = BibEntry(
        "wrong",
        "article",
        {
            "title": "Target Paper",
            "author": "Smith, Alice",
            "year": "2024",
            "eprint": "2401.12345",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "arxiv",
                "Different Paper",
                ["Someone Else"],
                2024,
                identifier="2401.12345",
            ),
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == LIKELY_HALLUCINATION
    assert "标识符" in result.reasons[0]


def test_identifier_conflict_can_offer_title_search_candidate():
    entry = BibEntry(
        "wrong",
        "article",
        {
            "title": "Target Paper",
            "author": "Smith, Alice",
            "year": "2024",
            "eprint": "2401.12345",
        },
    )

    class Provider:
        name = "fixture"

        def lookup_identifier(self, item):
            return [
                Candidate("fixture", "Different Paper", ["Someone Else"], 2024, identifier="2401.12345")
            ]

        def search_title(self, item):
            return [Candidate("fixture", "Target Paper", ["Alice Smith"], 2024)]

    result = check_entry(entry, Provider())
    assert result.status == NEEDS_REVIEW
    assert result.field_comparison["title"]["status"] == "mismatch"


def test_year_difference_needs_review_and_prints_both_values():
    entry = BibEntry(
        "year",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice",
            "year": "2023",
        },
    )
    provider = LocalProvider(
        [
            {
                "title": "A Real Paper",
                "authors": ["Alice Smith"],
                "year": 2024,
            }
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == NEEDS_REVIEW
    assert result.field_comparison["year"] == {
        "bibtex": 2023,
        "retrieved": 2024,
        "status": "mismatch",
    }
    assert any("Bib=2023" in reason for reason in result.reasons)


def test_weak_single_candidate_is_unconfirmed_not_hallucination():
    entry = BibEntry(
        "unknown",
        "article",
        {
            "title": "A Very Specific Paper",
            "author": "Smith, Alice",
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "A Different Paper",
                ["Someone Else"],
                2024,
            )
        ]
    )
    assert check_entry(entry, provider).status == UNCONFIRMED


def test_hallucination_count_only_includes_completed_sources():
    entry = BibEntry(
        "unknown",
        "article",
        {
            "title": (
                "Adaptive Reward Reweighting for Hierarchical Language "
                "Reasoning with Offline Demonstrations"
            ),
            "author": "Smith, Alice and Jones, Bob and Taylor, Carol",
            "year": "2024",
        },
    )

    class EmptyProvider:
        def __init__(self, name):
            self.name = name

        def search_title(self, item):
            return []

    class FailedProvider:
        name = "failed"

        def search_title(self, item):
            raise TimeoutError("timeout")

    result = check_entry(
        entry,
        [
            EmptyProvider("one"),
            EmptyProvider("two"),
            EmptyProvider("three"),
            FailedProvider(),
        ],
    )

    assert result.status == LIKELY_HALLUCINATION
    assert result.reasons[0].startswith("3 个已完成的")
    assert "failed:title" in result.provider_errors


def test_exact_fields_are_verified():
    entry = BibEntry(
        "complete",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice",
            "year": "2024",
            "journal": "Journal of Tests",
        },
    )
    provider = LocalProvider(
        [
            {
                "title": "A Real Paper",
                "authors": ["Alice Smith"],
                "year": 2024,
                "venue": "Journal of Tests",
            }
        ]
    )
    assert check_entry(entry, provider).status == VERIFIED


def test_author_name_order_and_initials_are_not_mismatches():
    entry = BibEntry(
        "authors",
        "article",
        {
            "title": "Policy Optimization",
            "author": (
                "Schulman, John and Levine, Sergey and Abbeel, Pieter "
                "and Jordan, Michael and Moritz, Philipp"
            ),
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "Policy Optimization",
                [
                    "John Schulman",
                    "Sergey Levine",
                    "Philipp Moritz",
                    "Michael I. Jordan",
                    "Pieter Abbeel",
                ],
                2024,
            )
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == NEEDS_REVIEW
    authors = result.field_comparison["authors"]
    assert authors["status"] == "minor_difference"
    assert not authors["added"]
    assert not authors["removed"]
    assert "Pieter Abbeel" in authors["reordered"][0]


def test_one_author_added_or_removed_is_summarized():
    entry = BibEntry(
        "authors",
        "article",
        {
            "title": "Policy Optimization",
            "author": "One, Alice and Two, Bob and Three, Carol",
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "Policy Optimization",
                ["Alice One", "Bob Two", "Carol Three", "Dan Added"],
                2024,
            )
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == NEEDS_REVIEW
    authors = result.field_comparison["authors"]
    assert authors["added"] == ["Dan Added（检索第4位）"]
    assert authors["removed"] == []
    assert any("Dan Added" in reason for reason in result.reasons)


def test_title_only_match_with_most_authors_different_is_unconfirmed():
    entry = BibEntry(
        "authors",
        "article",
        {
            "title": "Policy Optimization",
            "author": "One, Alice and Two, Bob and Three, Carol and Four, Dan",
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "Policy Optimization",
                ["Alice One", "X New", "Y New", "Z New"],
                2024,
            )
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == UNCONFIRMED
    assert result.field_comparison["authors"]["status"] == "major_mismatch"


def test_identifier_title_match_with_author_difference_needs_review():
    entry = BibEntry(
        "authors",
        "article",
        {
            "title": "Policy Optimization",
            "author": "One, Alice and Two, Bob",
            "year": "2024",
            "eprint": "2401.12345",
        },
    )
    provider = LocalProvider(
        [
            {
                "title": "Policy Optimization",
                "authors": ["Alice One", "Different Person"],
                "year": 2024,
                "eprint": "2401.12345",
            }
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == NEEDS_REVIEW
    assert result.field_comparison["authors"]["status"] == "major_mismatch"


def test_title_comparison_is_case_insensitive():
    entry = BibEntry(
        "case",
        "article",
        {
            "title": "Reinforce++: Stabilizing Critic-Free Policy Optimization",
            "author": "Hu, Jian",
            "year": "2025",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "REINFORCE++: STABILIZING CRITIC-FREE POLICY OPTIMIZATION",
                ["Jian Hu"],
                2025,
            )
        ]
    )

    result = check_entry(entry, provider)

    assert result.status == VALIDATED
    assert result.field_comparison["title"]["status"] == "match"


def test_same_named_method_with_revised_subtitle_is_not_needs_review():
    entry = BibEntry(
        "reinforce",
        "article",
        {
            "title": (
                "Reinforce++: An efficient rlhf algorithm with robustness "
                "to both prompt and reward models"
            ),
            "author": (
                "Hu, Jian and Liu, Jason Klein and Xu, Haotian and Shen, Wei"
            ),
            "year": "2025",
            "eprint": "2501.03262",
        },
    )
    provider = LocalProvider(
        [
            {
                "title": (
                    "REINFORCE++: Stabilizing Critic-Free Policy Optimization "
                    "with Global Advantage Normalization"
                ),
                "authors": [
                    "Jian Hu",
                    "Jason Klein Liu",
                    "Haotian Xu",
                    "Wei Shen",
                ],
                "year": 2025,
                "eprint": "2501.03262",
            }
        ]
    )

    result = check_entry(entry, provider)

    assert result.status == VALIDATED
    assert result.field_comparison["title"]["status"] == "match"


def test_two_sources_returning_only_unrelated_candidates_are_unconfirmed():
    entry = BibEntry(
        "unknown",
        "article",
        {
            "title": "A Highly Specific Method for Calibrating Lunar Robots",
            "author": "Smith, Alice",
            "year": "2024",
        },
    )
    providers = [
        FunctionProvider(
            lambda item: [
                Candidate("source-a", "Calibration Methods", ["Someone Else"], 2024)
            ],
            name="source-a",
        ),
        FunctionProvider(
            lambda item: [
                Candidate("source-b", "Robotics on the Moon", ["Another Author"], 2024)
            ],
            name="source-b",
        ),
    ]
    assert check_entry(entry, providers).status == UNCONFIRMED


def test_empty_sources_require_a_specific_title_for_likely_hallucination():
    providers = [
        LocalProvider([], name="source-a"),
        LocalProvider([], name="source-b"),
    ]
    generic = BibEntry(
        "generic",
        "article",
        {
            "title": "Policy Optimization",
            "author": "Smith, Alice",
            "year": "2024",
        },
    )
    specific = BibEntry(
        "specific",
        "article",
        {
            "title": "A Spectral Calibration Method for Autonomous Lunar Robots",
            "author": "Smith, Alice",
            "year": "2024",
        },
    )
    assert check_entry(generic, providers).status == UNCONFIRMED
    assert check_entry(specific, providers).status == LIKELY_HALLUCINATION


def test_reliable_discovery_is_selected_over_higher_scoring_noise():
    entry = BibEntry(
        "ranking",
        "article",
        {
            "title": "Reliable Candidate Selection for Bibliography Checking",
            "author": "Smith, Alice and Jones, Bob and Brown, Carol",
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "Reliable Candidate Selection for Bibliography Checking",
                ["Different Person", "Another Person", "Third Person"],
                2024,
            ),
            Candidate(
                "fixture",
                "Reliable Candidate Selection in Bibliography Checking",
                ["Alice Smith", "Bob Jones"],
                2024,
            ),
        ]
    )
    result = check_entry(entry, provider)
    assert result.status == NEEDS_REVIEW
    assert result.field_comparison["authors"]["retrieved"] == [
        "Alice Smith",
        "Bob Jones",
    ]


def test_json_contains_field_comparison():
    entry = BibEntry(
        "json",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice",
            "year": "2023",
        },
    )
    result = check_entry(
        entry,
        LocalProvider(
            [
                {
                    "title": "A Real Paper",
                    "authors": ["Alice Smith"],
                    "year": 2024,
                }
            ]
        ),
    )
    assert result.as_dict()["field_comparison"] == result.field_comparison


def test_added_and_removed_authors_include_positions():
    entry = BibEntry(
        "authors",
        "article",
        {
            "title": "Policy Optimization",
            "author": "One, Alice and Removed, Bob and Three, Carol",
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "Policy Optimization",
                ["Alice One", "Carol Three", "Dan Added"],
                2024,
            )
        ]
    )
    authors = check_entry(entry, provider).field_comparison["authors"]
    assert authors["added"] == ["Dan Added（检索第3位）"]
    assert authors["removed"] == ["Bob Removed（Bib第2位）"]


def test_latex_accents_are_treated_as_the_same_author():
    entry = BibEntry(
        "accented",
        "article",
        {
            "title": "Policy Optimization",
            "author": (
                r"Ahmadian, Arash and Cremer, Chris and Gall{\'e}, Matthias "
                r"and Fadaee, Marzieh and {\"U}st{\"u}n, Ahmet"
            ),
            "year": "2024",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "fixture",
                "Policy Optimization",
                [
                    "Arash Ahmadian",
                    "Chris Cremer",
                    "Matthias Gallé",
                    "Marzieh Fadaee",
                    "Ahmet Üstün",
                ],
                2024,
            )
        ]
    )
    result = check_entry(entry, provider)
    assert result.field_comparison["authors"]["status"] == "match"
    assert not result.field_comparison["authors"]["added"]
    assert not result.field_comparison["authors"]["removed"]


def test_renamed_arxiv_paper_with_matching_authors_is_not_hallucination():
    entry = BibEntry(
        "renamed",
        "article",
        {
            "title": "Old Descriptive Title for the Same Work",
            "author": "Smith, Alice and Jones, Bob",
            "eprint": "2401.12345",
        },
    )
    provider = FunctionProvider(
        lambda item: [
            Candidate(
                "arxiv",
                "A New and Completely Different Title",
                ["Alice Smith", "Bob Jones"],
                2024,
                identifier="2401.12345",
            )
        ]
    )
    assert check_entry(entry, provider).status != LIKELY_HALLUCINATION


def test_specific_title_can_be_hallucination_when_one_provider_fails():
    entry = BibEntry(
        "missing",
        "article",
        {
            "title": "Highly Specific Offline Alignment Method for Language Models",
            "author": "Smith, Alice",
            "year": "2025",
        },
    )

    class EmptyProvider:
        name = "empty"

        def lookup_identifier(self, item):
            return None

        def search_title(self, item):
            return []

    class FailedProvider(EmptyProvider):
        name = "failed"

        def search_title(self, item):
            raise RuntimeError("timeout")

    result = check_entry(
        entry,
        [EmptyProvider(), EmptyProvider(), FailedProvider()],
    )
    assert result.status == LIKELY_HALLUCINATION


def test_identifier_ranking_prefers_better_matching_revision():
    entry = BibEntry(
        "revision",
        "article",
        {
            "title": "Current Paper Title",
            "author": "Smith, Alice and Jones, Bob",
            "eprint": "2401.12345",
        },
    )

    class RevisionProvider:
        name = "arxiv"

        def lookup_identifier(self, item):
            return [
                Candidate(
                    "arxiv",
                    "Old Unrelated Looking Title",
                    ["Alice Smith"],
                    2024,
                    identifier="2401.12345",
                    raw={"arxiv_version": 1},
                ),
                Candidate(
                    "arxiv",
                    "Current Paper Title",
                    ["Alice Smith", "Bob Jones"],
                    2024,
                    identifier="2401.12345",
                    raw={"arxiv_version": 3},
                ),
            ]

        def search_title(self, item):
            return []

    assert check_entry(entry, RevisionProvider()).status == VALIDATED


def test_direct_url_match_stops_before_other_sources():
    entry = BibEntry(
        "linked",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice",
            "year": "2024",
            "url": "https://example.org/paper",
        },
    )
    calls = []

    class URLProvider:
        name = "url"

        def applies(self, item):
            return True

        def search_title(self, item):
            calls.append("url")
            return [Candidate("url", "A Real Paper", ["Alice Smith"], 2024)]

    class SlowProvider:
        name = "slow"

        def search_title(self, item):
            calls.append("slow")
            return []

    result = check_entry(entry, [URLProvider(), SlowProvider()])

    assert result.status == VALIDATED
    assert calls == ["url"]


def test_notion_blog_is_validated_as_a_nonacademic_reference():
    entry = BibEntry(
        "simplerl",
        "misc",
        {
            "title": "A Notion Research Blog",
            "author": "Smith, Alice and Jones, Bob",
            "year": "2025",
            "howpublished": r"\url{https://example.notion.site/paper}",
            "note": "Notion Blog",
        },
    )

    class NotionProvider:
        name = "url"

        def applies(self, item):
            return True

        def search_title(self, item):
            return [
                Candidate(
                    "notion",
                    "A Notion Research Blog",
                    ["Alice Smith", "Bob Jones"],
                    2025,
                    venue="Notion Blog",
                    url="https://example.notion.site/paper",
                )
            ]

    result = check_entry(entry, NotionProvider())

    assert result.status == VALIDATED
    assert "真实网页/博客" in result.reasons[0]


def test_arxiv_identifier_match_stops_before_title_searches():
    entry = BibEntry(
        "arxiv",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice",
            "year": "2024",
            "eprint": "2401.12345",
        },
    )
    calls = []

    class Arxiv:
        name = "arxiv"

        def lookup_identifier(self, item):
            calls.append("arxiv-id")
            return [
                Candidate(
                    "arxiv",
                    "A Real Paper",
                    ["Alice Smith"],
                    2024,
                    identifier="2401.12345",
                )
            ]

        def search_title(self, item):
            calls.append("arxiv-title")
            return []

    class SlowProvider:
        name = "slow"

        def search_title(self, item):
            calls.append("slow")
            return []

    result = check_entry(entry, [Arxiv(), SlowProvider()])

    assert result.status == VALIDATED
    assert calls == ["arxiv-id"]


def test_official_venue_match_stops_before_general_search():
    entry = BibEntry(
        "conference",
        "inproceedings",
        {
            "title": "A Real Conference Paper",
            "author": "Smith, Alice",
            "year": "2025",
            "booktitle": "ICLR",
        },
    )
    calls = []

    class Official:
        name = "iclr"
        authoritative = True

        def applies(self, item):
            return True

        def search_title(self, item):
            calls.append("iclr")
            return [
                Candidate(
                    "iclr",
                    "A Real Conference Paper",
                    ["Alice Smith"],
                    2025,
                    venue="ICLR",
                )
            ]

    class General:
        name = "crossref"

        def search_title(self, item):
            calls.append("crossref")
            return []

    result = check_entry(entry, [Official(), General()])

    assert result.status == VALIDATED
    assert calls == ["iclr"]


def test_official_venue_year_miss_is_likely_hallucination():
    entry = BibEntry(
        "missing",
        "inproceedings",
        {
            "title": "Beyond Reward: Offline Preference-guided Policy Learning",
            "author": "Singh, Aviral and Hong, Joey and Kumar, Aviral",
            "year": "2023",
            "booktitle": "Advances in Neural Information Processing Systems",
        },
    )

    class Official:
        name = "neurips"
        authoritative = True

        def applies(self, item):
            return True

        def search_title(self, item):
            return []

    result = check_entry(entry, Official())

    assert result.status == LIKELY_HALLUCINATION
    assert "官方数据源" in result.reasons[0]


def test_official_miss_stops_before_general_search_and_hides_prior_failures():
    entry = BibEntry(
        "missing",
        "inproceedings",
        {
            "title": "Beyond Reward: Offline Preference-guided Policy Learning",
            "author": "Singh, Aviral and Hong, Joey and Kumar, Aviral",
            "year": "2023",
            "booktitle": "Advances in Neural Information Processing Systems",
            "eprint": "2301.12345",
        },
    )
    calls = []

    class FailedIdentifier:
        name = "arxiv"
        identifier_lookup = True

        def lookup_identifier(self, item):
            calls.append("identifier")
            raise TimeoutError("timeout")

        def search_title(self, item):
            return []

    class Official:
        name = "neurips"
        authoritative = True

        def search_title(self, item):
            calls.append("neurips")
            return []

    class General:
        name = "crossref"

        def search_title(self, item):
            calls.append("crossref")
            return []

    result = check_entry(
        entry,
        [FailedIdentifier(), Official(), General()],
    )

    assert result.status == LIKELY_HALLUCINATION
    assert calls == ["identifier", "neurips"]
    assert result.provider_errors == {}


def test_dataset_word_in_academic_title_does_not_disable_official_miss():
    entry = BibEntry(
        "missing",
        "inproceedings",
        {
            "title": (
                "Leveraging Offline Datasets for Efficient Online RL "
                "in Large Language Models"
            ),
            "author": "Mitchell, Eric and Levine, Sergey and Finn, Chelsea",
            "year": "2024",
            "booktitle": (
                "Proceedings of the International Conference on Machine Learning"
            ),
        },
    )

    class Official:
        name = "icml"
        authoritative = True

        def applies(self, item):
            return True

        def search_title(self, item):
            return []

    result = check_entry(entry, Official())

    assert result.status == LIKELY_HALLUCINATION
    assert "官方数据源" in result.reasons[0]


def test_general_sources_run_in_parallel():
    entry = BibEntry(
        "parallel",
        "article",
        {
            "title": "A Specific Parallel Search Paper for Bibliography Checking",
            "author": "Smith, Alice",
            "year": "2024",
        },
    )

    class SlowProvider:
        authoritative = False

        def __init__(self, name):
            self.name = name

        def search_title(self, item):
            time.sleep(0.08)
            return []

    start = time.perf_counter()
    check_entry(
        entry,
        [
            SlowProvider("one"),
            SlowProvider("two"),
            SlowProvider("three"),
        ],
    )
    elapsed = time.perf_counter() - start

    assert elapsed < 0.18
