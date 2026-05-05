"""Tests for the bibliography (.bib) parser."""

from __future__ import annotations

from rrxiv.parser.bibliography import parse_bib, parse_thebibliography


def test_arxiv_entry() -> None:
    text = """
@article{tao2024,
  author = {Tao, Terence},
  title = {Example title},
  year = {2024},
  eprint = {2404.12345},
  archivePrefix = {arXiv}
}
"""
    entries = parse_bib(text)
    assert len(entries) == 1
    e = entries[0]
    assert e.key == "tao2024"
    assert e.entry_type == "article"
    assert e.fields["author"] == "Tao, Terence"
    assert e.fields["year"] == "2024"
    assert e.arxiv_id == "2404.12345"
    assert e.doi is None


def test_doi_entry() -> None:
    text = """
@article{smith2023,
  author = {Smith, J.},
  title = {Foo},
  year = {2023},
  doi = {10.1234/example.5678}
}
"""
    entries = parse_bib(text)
    assert len(entries) == 1
    assert entries[0].doi == "10.1234/example.5678"
    assert entries[0].arxiv_id is None


def test_url_entry() -> None:
    text = """
@misc{rrxiv-cir-schema,
  author = {{rrxiv Project}},
  title = {{Canonical Intermediate Representation Schema, v0.1.0}},
  year = {2026},
  howpublished = {\\url{https://rrxiv.com/schema/v0/cir.schema.json}},
  note = {JSON Schema 2020-12 definition.}
}
"""
    entries = parse_bib(text)
    assert len(entries) == 1
    e = entries[0]
    assert e.key == "rrxiv-cir-schema"
    assert "rrxiv.com" in (e.url or "")


def test_arxiv_from_url() -> None:
    text = """
@misc{x,
  url = {https://arxiv.org/abs/2305.12345}
}
"""
    entries = parse_bib(text)
    assert entries[0].arxiv_id == "2305.12345"


def test_multiple_entries() -> None:
    text = """
@article{a, year = {2020}}
@misc{b, year = {2021}}
@inproceedings{c, year = {2022}}
"""
    entries = parse_bib(text)
    assert [e.key for e in entries] == ["a", "b", "c"]
    assert [e.entry_type for e in entries] == ["article", "misc", "inproceedings"]


def test_empty_bib() -> None:
    assert parse_bib("") == []


# ---- thebibliography (inline) ----


def test_thebibliography_simple() -> None:
    tex = r"""
\begin{document}
Body text.

\begin{thebibliography}{99}
\bibitem[Tao 2024]{tao2024}
T. Tao. \textit{Example title}. arXiv:2404.12345, 2024.

\bibitem[Smith 2023]{smith2023}
J. Smith. Foo. doi:10.1234/example.5678.
\end{thebibliography}

\end{document}
"""
    entries = parse_thebibliography(tex)
    assert len(entries) == 2

    by_key = {e.key: e for e in entries}
    assert by_key["tao2024"].fields["label"] == "Tao 2024"
    assert by_key["tao2024"].fields["eprint"] == "2404.12345"
    assert by_key["tao2024"].arxiv_id == "2404.12345"
    assert by_key["smith2023"].doi == "10.1234/example.5678"


def test_thebibliography_no_label() -> None:
    tex = r"""
\begin{thebibliography}{99}
\bibitem{plain-key} A plain entry with no [label].
\end{thebibliography}
"""
    entries = parse_thebibliography(tex)
    assert len(entries) == 1
    assert entries[0].key == "plain-key"
    assert "label" not in entries[0].fields


def test_thebibliography_extracts_url() -> None:
    tex = r"""
\begin{thebibliography}{99}
\bibitem{web-source}
Web reference. https://example.org/paper.html
\end{thebibliography}
"""
    entries = parse_thebibliography(tex)
    assert entries[0].url == "https://example.org/paper.html"


def test_thebibliography_no_block_returns_empty() -> None:
    tex = r"\begin{document}No bibliography here.\end{document}"
    assert parse_thebibliography(tex) == []


def test_thebibliography_multiple_blocks() -> None:
    """Some papers split bibliography across appendices; handle gracefully."""
    tex = r"""
\begin{thebibliography}{99}
\bibitem{a} First.
\end{thebibliography}

\section{Appendix}

\begin{thebibliography}{99}
\bibitem{b} Second.
\end{thebibliography}
"""
    entries = parse_thebibliography(tex)
    assert {e.key for e in entries} == {"a", "b"}
