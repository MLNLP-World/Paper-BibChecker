from bibchecker.models import BibEntry, Candidate
from bibchecker.providers import (
    ACLAnthologyProvider,
    ACLProceedingsProvider,
    ArxivProvider,
    CrossrefProvider,
    CVPRProceedingsProvider,
    DataCiteProvider,
    DBLPProvider,
    DirectURLProvider,
    ECCVProceedingsProvider,
    EMNLPProceedingsProvider,
    GitHubProvider,
    ICLRProceedingsProvider,
    ICCVProceedingsProvider,
    ICMLProceedingsProvider,
    JMLRProvider,
    NeurIPSProceedingsProvider,
    OpenReviewProvider,
    SemanticScholarProvider,
    default_providers,
)


class StubDBLP(DBLPProvider):
    def _json(self, url):
        return {
            "result": {
                "hits": {
                    "hit": [
                        {
                            "info": {
                                "title": "A Real Paper.",
                                "authors": {
                                    "author": [
                                        {"text": "Alice Smith 0001"},
                                        {"text": "Bob Jones"},
                                    ]
                                },
                                "year": "2024",
                                "venue": "ICLR",
                                "url": "https://dblp.org/rec/conf/iclr/test",
                                "doi": "10.1000/example",
                            }
                        }
                    ]
                }
            }
        }


class StubGitHub(GitHubProvider):
    def _json(self, url):
        return {
            "name": "paper-code",
            "full_name": "alice/paper-code",
            "owner": {"login": "alice"},
            "created_at": "2024-01-02T00:00:00Z",
            "html_url": "https://github.com/alice/paper-code",
        }


def test_dblp_provider_parses_publication():
    entry = BibEntry("paper", "article", {"title": "A Real Paper"})
    candidate = StubDBLP().search_title(entry)[0]
    assert candidate.title == "A Real Paper."
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2024
    assert candidate.venue == "ICLR"


def test_dblp_skips_entries_with_stable_identifiers_or_known_venue():
    arxiv_entry = BibEntry(
        "arxiv",
        "article",
        {"title": "A Real Paper", "eprint": "2401.12345"},
    )
    doi_entry = BibEntry(
        "doi",
        "article",
        {"title": "A Real Paper", "doi": "10.1000/example"},
    )
    venue_entry = BibEntry(
        "venue",
        "inproceedings",
        {"title": "A Real Paper", "booktitle": "ICLR"},
    )

    provider = DBLPProvider()

    assert not provider.applies(arxiv_entry)
    assert not provider.applies(doi_entry)
    assert not provider.applies(venue_entry)


def test_github_provider_only_activates_for_github_url():
    plain = BibEntry("plain", "misc", {"title": "Paper Code"})
    github = BibEntry(
        "github",
        "misc",
        {
            "title": "Paper Code",
            "url": "https://github.com/alice/paper-code",
        },
    )
    provider = StubGitHub()
    assert provider.lookup_identifier(plain) is None
    candidate = provider.lookup_identifier(github)[0]
    assert candidate.identifier == "alice/paper-code"


class StubArxiv(ArxivProvider):
    def _abs_candidate(self, identifier):
        self.identifiers = getattr(self, "identifiers", [])
        self.identifiers.append(identifier)
        return Candidate(
            "arxiv",
            "A Real Paper",
            ["Alice Smith"],
            2024,
            identifier="2401.12345",
            raw={"arxiv_version": 1 if identifier.endswith("v1") else None},
        )


def test_arxiv_identifier_lookup_reads_latest_and_first_version():
    entry = BibEntry(
        "paper",
        "article",
        {"title": "Paper", "eprint": "2401.12345"},
    )
    provider = StubArxiv()
    candidates = provider.lookup_identifier(entry)
    assert set(provider.identifiers) == {"2401.12345", "2401.12345v1"}
    assert len(candidates) == 2


class StubArxivAbsPage(ArxivProvider):
    def _get(self, url, accept="application/json"):
        self.url = url
        return b"""
        <html><head>
          <meta name="citation_title" content="A Real Paper">
          <meta name="citation_author" content="Smith, Alice">
          <meta name="citation_author" content="Jones, Bob">
          <meta name="citation_date" content="2024/01/02">
          <meta name="citation_arxiv_id" content="2401.12345">
          <meta name="citation_doi" content="10.1000/example">
        </head></html>
        """


def test_arxiv_abs_page_parses_citation_metadata():
    candidate = StubArxivAbsPage()._abs_candidate("2401.12345v1")

    assert candidate.title == "A Real Paper"
    assert candidate.authors == ["Smith, Alice", "Jones, Bob"]
    assert candidate.year == 2024
    assert candidate.identifier == "2401.12345"
    assert candidate.raw["doi"] == "10.1000/example"
    assert candidate.raw["arxiv_version"] == 1


class StubCrossref(CrossrefProvider):
    def _json(self, url):
        self.url = url
        return {"message": {"items": []}}


def test_crossref_title_search_uses_first_author():
    entry = BibEntry(
        "paper",
        "article",
        {"title": "A Real Paper", "author": "Smith, Alice and Jones, Bob"},
    )
    provider = StubCrossref()
    provider.search_title(entry)
    assert "query.author=Smith" in provider.url


class StubSemanticScholar(SemanticScholarProvider):
    def _json(self, url):
        self.urls = getattr(self, "urls", [])
        self.urls.append(url)
        paper = {
            "paperId": "s2-paper-id",
            "title": "A Real Paper",
            "authors": [{"name": "Alice Smith"}, {"name": "Bob Jones"}],
            "year": 2024,
            "venue": "ICLR",
            "publicationVenue": {"name": "International Conference on Learning Representations"},
            "externalIds": {
                "DOI": "10.1000/example",
                "ArXiv": "2401.12345",
            },
            "url": "https://www.semanticscholar.org/paper/s2-paper-id",
        }
        if "/search/match?" in url:
            return {"data": [paper]}
        return paper


def test_semantic_scholar_identifier_lookup_uses_doi_and_arxiv():
    entry = BibEntry(
        "paper",
        "article",
        {
            "title": "A Real Paper",
            "doi": "10.1000/example",
            "eprint": "2401.12345",
        },
    )
    provider = StubSemanticScholar()
    candidates = provider.lookup_identifier(entry)

    assert len(candidates) == 2
    assert "/paper/DOI:10.1000%2Fexample?" in provider.urls[0]
    assert "/paper/ARXIV:2401.12345?" in provider.urls[1]
    assert candidates[0].title == "A Real Paper"
    assert candidates[0].authors == ["Alice Smith", "Bob Jones"]
    assert candidates[0].identifier == "10.1000/example"


def test_semantic_scholar_title_search_uses_match_endpoint():
    entry = BibEntry("paper", "article", {"title": "A Real Paper"})
    provider = StubSemanticScholar()
    candidate = provider.search_title(entry)[0]

    assert "/paper/search/match?" in provider.urls[0]
    assert "query=A+Real+Paper" in provider.urls[0]
    assert candidate.source == "semanticscholar"
    assert candidate.venue == "ICLR"


def test_semantic_scholar_api_key_uses_s2_header():
    headers = SemanticScholarProvider(token="secret")._headers(
        "application/json",
        "Paper-BibChecker/0.1",
    )

    assert headers["x-api-key"] == "secret"
    assert "Authorization" not in headers


def test_semantic_scholar_accepts_s2_api_key_alias(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.setenv("S2_API_KEY", "alias-secret")

    provider = SemanticScholarProvider()

    assert provider.token == "alias-secret"


def test_default_providers_skip_semantic_scholar_without_key(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)

    names = [provider.name for provider in default_providers()]

    assert "semanticscholar" not in names


def test_default_providers_include_semantic_scholar_with_key(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "secret")

    names = [provider.name for provider in default_providers()]

    assert "semanticscholar" in names


class StubOpenReview(OpenReviewProvider):
    def _json(self, url):
        return {
            "notes": [
                {
                    "id": "note-id",
                    "forum": "forum-id",
                    "pdate": 1711064894711,
                    "content": {
                        "title": {"value": "A Real Paper"},
                        "authors": {"value": ["Alice Smith", "Bob Jones"]},
                        "venue": {"value": "COLM"},
                    },
                },
                {
                    "id": "review-id",
                    "content": {"summary": {"value": "A review without a title"}},
                },
            ]
        }


def test_openreview_provider_parses_submission_and_skips_reviews():
    entry = BibEntry("paper", "article", {"title": "A Real Paper"})
    candidates = StubOpenReview().search_title(entry)
    assert len(candidates) == 1
    assert candidates[0].title == "A Real Paper"
    assert candidates[0].authors == ["Alice Smith", "Bob Jones"]
    assert candidates[0].venue == "COLM"
    assert candidates[0].url.endswith("forum-id")


class StubOpenReviewVenuePreference(OpenReviewProvider):
    def _json(self, url):
        return {
            "notes": [
                {
                    "id": "corr-id",
                    "forum": "corr-forum",
                    "pdate": 1736000000000,
                    "content": {
                        "title": {"value": "SimpleRL-Zoo"},
                        "authors": {"value": ["Weihao Zeng"]},
                        "venue": {"value": "CoRR 2025"},
                    },
                },
                {
                    "id": "colm-id",
                    "forum": "colm-forum",
                    "pdate": 1736000000000,
                    "content": {
                        "title": {"value": "SimpleRL-Zoo"},
                        "authors": {"value": ["Weihao Zeng"]},
                        "venue": {"value": "COLM 2025"},
                    },
                },
            ]
        }


def test_openreview_prefers_bib_venue_over_corr_duplicate():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "SimpleRL-Zoo",
            "author": "Weihao Zeng",
            "booktitle": "Second Conference on Language Modeling",
            "year": "2025",
        },
    )
    candidates = StubOpenReviewVenuePreference(max_results=1).search_title(entry)
    assert candidates[0].venue == "COLM 2025"
    assert candidates[0].url.endswith("colm-forum")


class StubDataCite(DataCiteProvider):
    def _json(self, url):
        return {
            "data": {
                "attributes": {
                    "titles": [{"title": "A Real Paper"}],
                    "creators": [{"name": "Smith, Alice"}],
                    "publicationYear": 2024,
                    "publisher": "arXiv",
                    "url": "https://arxiv.org/abs/2401.12345",
                }
            }
        }


def test_datacite_provider_resolves_arxiv_identifier():
    entry = BibEntry(
        "paper",
        "article",
        {"title": "A Real Paper", "eprint": "2401.12345"},
    )
    candidate = StubDataCite().lookup_identifier(entry)[0]
    assert candidate.title == "A Real Paper"
    assert candidate.authors == ["Smith, Alice"]
    assert candidate.identifier == "2401.12345"


class StubACLAnthology(ACLAnthologyProvider):
    def _get(self, url, accept="application/json"):
        return b"""
        @article{smith-2023-paper,
          title = {A Real Paper},
          author = {Smith, Alice and Jones, Bob},
          journal = {Transactions of the Association for Computational Linguistics},
          year = {2023},
        }
        """


def test_acl_anthology_provider_reads_official_volume_bib():
    entry = BibEntry(
        "paper",
        "article",
        {
            "title": "A Real Paper",
            "journal": "Transactions of the Association for Computational Linguistics",
            "year": "2023",
        },
    )
    candidate = StubACLAnthology().search_title(entry)[0]
    assert candidate.title == "A Real Paper"
    assert candidate.authors == ["Smith, Alice", "Jones, Bob"]
    assert candidate.year == 2023


class StubJMLR(JMLRProvider):
    def _get(self, url, accept="application/json"):
        return b"""
        <dl>
        <dt>A Real Paper</dt>
        <dd><b><i>Alice Smith, Bob Jones</i></b>; (45):1&minus;58, 2025.
        <br>[<a href='/papers/v26/24-0001.html'>abs</a>]
        </dl>
        """


def test_jmlr_provider_reads_official_volume_index():
    entry = BibEntry(
        "paper",
        "article",
        {
            "title": "A Real Paper",
            "journal": "Journal of Machine Learning Research",
            "volume": "26",
        },
    )
    candidate = StubJMLR().search_title(entry)[0]
    assert candidate.title == "A Real Paper"
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2025


class StubBookProceedings:
    def _get(self, url, accept="application/json"):
        self.url = url
        return b"""
        <ul class="paper-list">
          <li class="conference">(2025)
            <a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">
              A Real Paper
            </a>
            Alice Smith, Bob Jones
          </li>
        </ul>
        """


class StubICLR(StubBookProceedings, ICLRProceedingsProvider):
    pass


class StubNeurIPS(StubBookProceedings, NeurIPSProceedingsProvider):
    pass


def test_iclr_official_search_parses_result_and_applies_by_venue():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "author": "Smith, Alice and Jones, Bob",
            "booktitle": "International Conference on Learning Representations",
            "year": "2025",
        },
    )
    provider = StubICLR()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2025
    assert candidate.venue == "International Conference on Learning Representations"
    assert "/papers/search?" in provider.url


def test_neurips_official_search_parses_result_and_applies_by_venue():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "Advances in Neural Information Processing Systems",
            "year": "2025",
        },
    )
    provider = StubNeurIPS()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert candidate.source == "neurips"


def test_neurips_applies_to_full_proceedings_name():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Paper",
            "booktitle": "Advances in Neural Information Processing Systems",
            "year": "2023",
        },
    )
    assert NeurIPSProceedingsProvider().applies(entry)


class StubICML(ICMLProceedingsProvider):
    def _get(self, url, accept="application/json"):
        if url.rstrip("/") == self.endpoint:
            return b"""
            <ul>
              <li><a href="v267"><b>Volume 267</b></a>
              Proceedings of ICML 2025</li>
            </ul>
            """
        self.volume_url = url
        return b"""
        <div class="paper">
          <p class="title">A Real Paper</p>
          <p class="details">
            <span class="authors">Alice Smith,&nbsp;Bob Jones</span>;
          </p>
          <p class="links">
            [<a href="https://proceedings.mlr.press/v267/smith25a.html">abs</a>]
          </p>
        </div>
        """


def test_icml_official_provider_finds_volume_and_parses_index():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "International Conference on Machine Learning",
            "year": "2025",
        },
    )
    provider = StubICML()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert provider.volume_url.endswith("/v267/")
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2025


def test_icml_known_year_uses_stable_volume_mapping():
    assert ICMLProceedingsProvider()._volume_for_year(2015) == 37
    assert ICMLProceedingsProvider()._volume_for_year(2024) == 235


class StubICMLAdjacentYear(ICMLProceedingsProvider):
    def __init__(self):
        super().__init__()
        self.requested_years = []

    def _volume_for_year(self, year):
        self.requested_years.append(year)
        volumes = {2024: 235, 2025: 267, 2023: 202}
        return volumes[year]

    def _get(self, url, accept="application/json"):
        if "/v202/" in url:
            title = "A Real Paper"
            paper_url = "smith23a.html"
        else:
            title = "An Unrelated Paper"
            paper_url = "other.html"
        return f"""
        <div class="paper">
          <p class="title">{title}</p>
          <p class="details">
            <span class="authors">Alice Smith,&nbsp;Bob Jones</span>;
          </p>
          <p class="links">
            [<a href="{paper_url}">abs</a>]
          </p>
        </div>
        """.encode()


def test_icml_searches_previous_publication_year_after_bib_year_miss():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "International Conference on Machine Learning",
            "year": "2024",
        },
    )
    provider = StubICMLAdjacentYear()
    candidate = provider.search_title(entry)[0]
    assert provider.requested_years == [2024, 2023]
    assert candidate.title == "A Real Paper"
    assert candidate.year == 2023
    assert candidate.url.endswith("/v202/smith23a.html")


def test_icml_does_not_search_adjacent_year_when_bib_year_matches():
    provider = StubICMLAdjacentYear()
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "An Unrelated Paper",
            "booktitle": "International Conference on Machine Learning",
            "year": "2024",
        },
    )
    candidate = provider.search_title(entry)[0]
    assert provider.requested_years == [2024]
    assert candidate.year == 2024


class StubICMLSimilarTitleInBibYear(StubICMLAdjacentYear):
    def _get(self, url, accept="application/json"):
        title = (
            "Beyond Reward: Offline Preference-guided Policy Learning"
            if "/v202/" in url
            else "Beyond Reward: Offline Preference-guided Policy Optimization"
        )
        return f"""
        <div class="paper">
          <p class="title">{title}</p>
          <p class="details">
            <span class="authors">Alice Smith,&nbsp;Bob Jones</span>;
          </p>
          <p class="links">
            [<a href="paper.html">abs</a>]
          </p>
        </div>
        """.encode()


def test_icml_checks_previous_year_after_only_similar_title_in_bib_year():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "Beyond Reward: Offline Preference-guided Policy Learning",
            "booktitle": "International Conference on Machine Learning",
            "year": "2024",
        },
    )
    provider = StubICMLSimilarTitleInBibYear()
    candidate = provider.search_title(entry)[0]
    assert provider.requested_years == [2024, 2023]
    assert candidate.title.endswith("Policy Learning")
    assert candidate.year == 2023


class StubICMLMissingPreviousYear(StubICMLAdjacentYear):
    def _volume_for_year(self, year):
        self.requested_years.append(year)
        if year == 2023:
            raise ValueError("missing volume")
        return 235

    def _get(self, url, accept="application/json"):
        return f"""
        <div class="paper">
          <p class="title">An Unrelated Paper</p>
          <p class="details">
            <span class="authors">Alice Smith,&nbsp;Bob Jones</span>;
          </p>
          <p class="links">
            [<a href="paper.html">abs</a>]
          </p>
        </div>
        """.encode()


def test_icml_skips_missing_previous_volume():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "International Conference on Machine Learning",
            "year": "2024",
        },
    )
    provider = StubICMLMissingPreviousYear()
    candidate = provider.search_title(entry)[0]
    assert provider.requested_years == [2024, 2023]
    assert candidate.year == 2024


class StubICMLMissingAllYears(StubICMLAdjacentYear):
    def _volume_for_year(self, year):
        self.requested_years.append(year)
        raise ValueError(f"missing {year}")


def test_icml_reports_error_when_no_nearby_official_volume_exists():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "International Conference on Machine Learning",
            "year": "2024",
        },
    )
    provider = StubICMLMissingAllYears()
    try:
        provider.search_title(entry)
    except ValueError as error:
        assert str(error) == "missing 2023"
    else:
        raise AssertionError("expected missing-volume error")


def test_icml_applies_to_proceedings_prefixed_full_name():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Paper",
            "booktitle": (
                "Proceedings of the International Conference on Machine Learning"
            ),
            "year": "2024",
        },
    )
    assert ICMLProceedingsProvider().applies(entry)


class StubAnthologyVenue:
    volume_id = ""

    def _get(self, url, accept="application/json"):
        if "/venues/" in url:
            return (
                f'<a href="/volumes/{self.volume_id}/">volume</a>'
            ).encode()
        self.bib_url = url
        return b"""
        @inproceedings{smith-2025-paper,
          title = {A Real Paper},
          author = {Smith, Alice and Jones, Bob},
          year = {2025},
        }
        """


class StubACLVenue(StubAnthologyVenue, ACLProceedingsProvider):
    volume_id = "2025.acl-long"


class StubEMNLPVenue(StubAnthologyVenue, EMNLPProceedingsProvider):
    volume_id = "2025.emnlp-main"


def test_acl_official_provider_uses_venue_volume_bib():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "Annual Meeting of the Association for Computational Linguistics",
            "year": "2025",
        },
    )
    provider = StubACLVenue()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert provider.bib_url.endswith("/2025.acl-long.bib")
    assert candidate.source == "acl"


def test_emnlp_official_provider_uses_venue_volume_bib():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "EMNLP",
            "year": "2025",
        },
    )
    provider = StubEMNLPVenue()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert provider.bib_url.endswith("/2025.emnlp-main.bib")
    assert candidate.source == "emnlp"


def test_default_providers_include_requested_official_sources():
    names = {provider.name for provider in default_providers()}
    assert {
        "iclr",
        "neurips",
        "icml",
        "acl",
        "emnlp",
        "cvpr",
        "iccv",
        "eccv",
    } <= names


class StubDirectURL(DirectURLProvider):
    def _get(self, url, accept="application/json"):
        self.url = url
        return b"""
        <html><head>
          <meta name="citation_title" content="A Real Paper">
          <meta name="citation_author" content="Alice Smith">
          <meta name="citation_author" content="Bob Jones">
          <meta name="citation_publication_date" content="2024-06-01">
          <meta name="citation_conference_title" content="ICLR">
          <meta name="citation_doi" content="10.1000/example">
        </head></html>
        """


def test_direct_url_provider_reads_citation_metadata():
    entry = BibEntry(
        "paper",
        "article",
        {
            "title": "A Real Paper",
            "url": "https://example.org/paper",
        },
    )
    candidate = StubDirectURL().search_title(entry)[0]
    assert candidate.title == "A Real Paper"
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2024
    assert candidate.venue == "ICLR"
    assert candidate.identifier == "10.1000/example"


class StubNotion(DirectURLProvider):
    def _get(self, url, accept="application/json"):
        return """
        <html>
          <head>
            <meta property="og:title"
              content="7B Model and 8K Examples: Emerging Reasoning with Reinforcement Learning is Both Effective and Efficient | Notion">
          </head>
          <body>
            <p><a href="https://github.com/Zeng-WH"><strong>Weihao Zeng</strong></a>,
            <a href="https://hyz17.github.io">Yuzhen Huang</a>,
            Wei Liu, Keqing He, Qian Liu, Zejun Ma, Junxian He</p>
            <p>— Jan 25, 2025</p>
          </body>
        </html>
        """.encode()


def test_notion_blog_url_provider_reads_title_authors_and_date():
    entry = BibEntry(
        "simplerl",
        "misc",
        {
            "title": (
                "7B Model and 8K Examples: Emerging Reasoning with "
                "Reinforcement Learning is Both Effective and Efficient"
            ),
            "author": (
                "Weihao Zeng and Yuzhen Huang and Wei Liu and Keqing He "
                "and Qian Liu and Zejun Ma and Junxian He"
            ),
            "year": "2025",
            "howpublished": r"\url{https://hkust-nlp.notion.site/simplerl-reason}",
            "note": "Notion Blog",
        },
    )
    provider = StubNotion()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert candidate.source == "notion"
    assert candidate.title.startswith("7B Model and 8K Examples")
    assert candidate.authors[:2] == ["Weihao Zeng", "Yuzhen Huang"]
    assert len(candidate.authors) == 7
    assert candidate.year == 2025
    assert candidate.venue == "Notion Blog"


class StubCVF:
    def _get(self, url, accept="application/json"):
        self.url = url
        return b"""
        <dt class="ptitle"><br>
          <a href="/content/CVPR2025/html/Smith_A_Real_Paper_CVPR_2025_paper.html">
            A Real Paper
          </a>
        </dt>
        <dd>
          <form><input type="hidden" name="query_author" value="Alice Smith"></form>
          <form><input type="hidden" name="query_author" value="Bob Jones"></form>
        </dd>
        """


class StubCVPR(StubCVF, CVPRProceedingsProvider):
    pass


class StubICCV(StubCVF, ICCVProceedingsProvider):
    pass


def test_cvpr_official_provider_parses_open_access_list():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "CVPR",
            "year": "2025",
        },
    )
    provider = StubCVPR()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert provider.url.endswith("/CVPR2025?day=all")
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.source == "cvpr"


def test_iccv_official_provider_uses_iccv_year_page():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "ICCV",
            "year": "2025",
        },
    )
    provider = StubICCV()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert provider.url.endswith("/ICCV2025?day=all")
    assert candidate.source == "iccv"


class StubECCV(ECCVProceedingsProvider):
    def _get(self, url, accept="application/json"):
        return b"""
        <!-- ECCV 2024 -->
        <button>ECCV 2024 Papers</button>
        <div>
          <dt class="ptitle"><br>
            <a href=papers/eccv_2024/papers_ECCV/html/4_ECCV_2024_paper.php>
              A Real Paper
            </a>
          </dt>
          <dd>Alice Smith*, Bob Jones</dd>
        </div>
        <!-- ECCV 2022 -->
        """


def test_eccv_official_provider_parses_requested_year_section():
    entry = BibEntry(
        "paper",
        "inproceedings",
        {
            "title": "A Real Paper",
            "booktitle": "European Conference on Computer Vision",
            "year": "2024",
        },
    )
    provider = StubECCV()
    candidate = provider.search_title(entry)[0]
    assert provider.applies(entry)
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2024
    assert candidate.source == "eccv"
