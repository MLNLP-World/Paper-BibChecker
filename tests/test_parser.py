from bibchecker.parser import (
    find_citation_keys,
    find_uncited_entries,
    find_uncited_keys,
    parse_bib,
)


def test_parse_bib_entries_and_convenience_properties(tmp_path):
    bib_path = tmp_path / "references.bib"
    bib_path.write_text(
        r'''
        % 文件级注释。
        @string{venue = "Journal of " # "Examples"}
        @comment{This block is ignored.}
        @preamble{"ignored"}

        @Article{smith2024,
          title = {A {Nested, Protected} Title},
          author = {Smith, Alice and {Research and Development Team}},
          year = 2024,
          journal = venue,
          doi = {https://doi.org/10.1000/example.1},
          eprint = {arXiv:2401.12345v2},
        }

        @inproceedings(jones2023,
          title = "A quoted title",
          author = "Jones, Bob",
          year = {2023},
        )
        ''',
        encoding="utf-8",
    )

    entries = parse_bib(bib_path)

    assert set(entries) == {"smith2024", "jones2023"}
    article = entries["smith2024"]
    assert article.entry_type == "article"
    assert article.title == "A {Nested, Protected} Title"
    assert article.authors == [
        "Smith, Alice",
        "{Research and Development Team}",
    ]
    assert article.year == 2024
    assert article.doi == "10.1000/example.1"
    assert article.arxiv_id == "2401.12345v2"
    assert article.fields["journal"] == "Journal of Examples"

    proceedings = entries["jones2023"]
    assert proceedings.entry_type == "inproceedings"
    assert proceedings.title == "A quoted title"
    assert proceedings.authors == ["Jones, Bob"]


def test_find_citations_recursively_with_comments_and_cycles(tmp_path):
    sections = tmp_path / "sections"
    sections.mkdir()
    main = tmp_path / "main.tex"
    intro = sections / "intro.tex"
    appendix = tmp_path / "appendix.tex"

    main.write_text(
        r'''
        \cite[see][p.~4]{root, shared}
        \Citeauthor*{author-key}
        % \cite{commented-out}
        \verb|\cite{verbatim-inline}|
        \begin{verbatim}
        \cite{verbatim-block}
        \end{verbatim}
        \input{sections/intro}
        \include{appendix}
        \input{missing-file}
        ''',
        encoding="utf-8",
    )
    intro.write_text(
        r'''
        \parencite{child}
        \textcites{multi-one}[p.~2]{multi-two}
        \input{../main}
        ''',
        encoding="utf-8",
    )
    appendix.write_text(r"\nocite{appendix-only}", encoding="utf-8")

    assert find_citation_keys(main) == {
        "root",
        "shared",
        "author-key",
        "child",
        "multi-one",
        "multi-two",
        "appendix-only",
    }


def test_find_uncited_entries_and_nocite_wildcard(tmp_path):
    bib_path = tmp_path / "refs.bib"
    tex_path = tmp_path / "paper.tex"
    bib_path.write_text(
        """
        @article{used, title={Used}}
        @article{unused, title={Unused}}
        """,
        encoding="utf-8",
    )
    tex_path.write_text(r"\cite{used}", encoding="utf-8")

    entries = parse_bib(bib_path)
    assert find_uncited_keys(entries, {"used"}) == {"unused"}
    assert set(find_uncited_entries(bib_path, tex_path)) == {"unused"}
    assert find_uncited_keys(entries, {"*"}) == set()
