from bibchecker.matching import (
    assess_candidate,
    compare_authors,
    consolidate_candidates,
    rank_candidates,
)
from bibchecker.models import BibEntry, Candidate


def test_truncated_retrieved_authors_are_unknown_not_removed():
    comparison = compare_authors(
        ["Smith, Alice", "Jones, Bob", "Taylor, Carol", "Wang, Dan"],
        ["Alice Smith", "Bob Jones", "others"],
    )

    assert comparison["status"] == "partial_match"
    assert comparison["removed"] == []
    assert len(comparison["unobserved"]) == 2
    assert comparison["first_match"] is True


def test_truncated_bib_authors_do_not_make_observed_authors_added():
    comparison = compare_authors(
        ["Smith, Alice", "others"],
        ["Alice Smith", "Bob Jones", "Carol Taylor"],
    )

    assert comparison["status"] == "partial_match"
    assert comparison["added"] == []
    assert comparison["removed"] == []
    assert len(comparison["observed_extra"]) == 2


def test_added_author_is_minor_when_both_lists_are_complete():
    comparison = compare_authors(
        ["One, Alice", "Two, Bob", "Three, Carol"],
        ["Alice One", "Bob Two", "Carol Three", "Dan Added"],
    )

    assert comparison["status"] == "minor_difference"
    assert comparison["removed"] == []
    assert comparison["added"] == ["Dan Added（检索第4位）"]


def test_reordered_authors_match_by_identity_not_position():
    comparison = compare_authors(
        ["Schulman, John", "Levine, Sergey", "Abbeel, Pieter"],
        ["Sergey Levine", "Pieter Abbeel", "John Schulman"],
    )

    assert comparison["status"] == "minor_difference"
    assert comparison["overlap"] == 1.0
    assert len(comparison["reordered"]) == 3


def test_missing_author_metadata_is_not_a_mismatch():
    entry = BibEntry(
        "paper",
        "article",
        {"title": "A Real Paper", "author": "Smith, Alice", "year": "2024"},
    )
    assessment = assess_candidate(
        entry,
        Candidate("incomplete-source", "A Real Paper", [], None),
    )

    assert assessment.confidence == "title_only"
    assert assessment.author_score is None
    assert assessment.author_comparison["status"] == "not_available"
    assert assessment.identifier_conflict is False


def test_arxiv_version_numbers_match_but_wrong_record_is_conflict():
    entry = BibEntry(
        "paper",
        "article",
        {
            "title": "The Correct Paper",
            "author": "Smith, Alice",
            "eprint": "2401.12345v1",
        },
    )
    same_record = assess_candidate(
        entry,
        Candidate(
            "arxiv",
            "The Correct Paper",
            ["Alice Smith"],
            2024,
            identifier="arXiv:2401.12345v2",
        ),
    )
    wrong_record = assess_candidate(
        entry,
        Candidate(
            "arxiv",
            "A Completely Different Paper",
            ["Someone Else"],
            2024,
            identifier="2401.12345",
        ),
    )

    assert same_record.identifier_match is True
    assert same_record.identifier_conflict is False
    assert wrong_record.identifier_match is True
    assert wrong_record.identifier_conflict is True
    assert wrong_record.score < same_record.score


def test_incomplete_and_complete_sources_are_consolidated():
    entry = BibEntry(
        "paper",
        "article",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice and Jones, Bob",
            "year": "2024",
        },
    )
    groups = consolidate_candidates(
        entry,
        [
            Candidate("title-only", "A Real Paper", [], None),
            Candidate(
                "complete-source",
                "A Real Paper.",
                ["Alice Smith", "Bob Jones"],
                2024,
            ),
        ],
    )

    assert len(groups) == 1
    assert groups[0].sources == ("complete-source", "title-only")
    assert groups[0].representative.candidate.source == "complete-source"
    assert groups[0].score >= groups[0].representative.score


def test_rank_prefers_complete_match_over_title_only_duplicate():
    entry = BibEntry(
        "paper",
        "article",
        {"title": "A Real Paper", "author": "Smith, Alice"},
    )
    ranked = rank_candidates(
        entry,
        [
            Candidate("title-only", "A Real Paper", [], None),
            Candidate("author-source", "A Real Paper", ["Alice Smith"], None),
        ],
    )

    assert ranked[0].candidate.source == "author-source"
