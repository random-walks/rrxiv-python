"""Tests for the bibliography (.bib) parser."""

from __future__ import annotations

from rrvix.parser.bibliography import parse_bib


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
@misc{rrvix-cir-schema,
  author = {{rrvix Project}},
  title = {{Canonical Intermediate Representation Schema, v0.1.0}},
  year = {2026},
  howpublished = {\\url{https://rrvix.org/schema/v0/cir.schema.json}},
  note = {JSON Schema 2020-12 definition.}
}
"""
    entries = parse_bib(text)
    assert len(entries) == 1
    e = entries[0]
    assert e.key == "rrvix-cir-schema"
    assert "rrvix.org" in (e.url or "")


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
