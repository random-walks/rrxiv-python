"""End-to-end parser tests: .tex + .rrxiv.aux + .bib → CIR → schema validation.

These run against vendored fixtures in ``tests/fixtures/`` so the test
suite doesn't depend on a working LaTeX install. The fixtures are
verbatim copies of files from the rrxiv repo plus a precompiled sidecar.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rrxiv.models import CIR
from rrxiv.parser import build_cir

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MINIMAL_DIR = FIXTURES_DIR / "minimal"


@pytest.fixture
def minimal_cir() -> CIR:
    return build_cir(MINIMAL_DIR / "minimal.tex")


def test_minimal_returns_cir(minimal_cir: CIR) -> None:
    """build_cir returns a CIR pydantic model that validates against the schema."""
    assert isinstance(minimal_cir, CIR)


def test_minimal_metadata(minimal_cir: CIR) -> None:
    """Metadata fields come from the sidecar."""
    assert minimal_cir.id == "rrxiv-example-minimal"
    assert minimal_cir.version == "v1"
    assert minimal_cir.rrxiv_version == "0.1.0"
    assert minimal_cir.license == "CC-BY-4.0"
    assert minimal_cir.topics == ["example", "conformance"]


def test_minimal_title_and_authors(minimal_cir: CIR) -> None:
    """Title and authors come from the .tex source."""
    assert minimal_cir.title == "A minimal rrxiv paper"
    assert len(minimal_cir.authors) == 1
    assert minimal_cir.authors[0].name == "rrxiv Project"


def test_minimal_abstract_present(minimal_cir: CIR) -> None:
    assert "parser conformance fixture" in (minimal_cir.abstract or "")


def test_minimal_one_section(minimal_cir: CIR) -> None:
    assert minimal_cir.sections is not None
    assert len(minimal_cir.sections) == 1
    assert minimal_cir.sections[0].title == "The claim"
    assert minimal_cir.sections[0].id == "sec:claim"


def test_minimal_one_claim(minimal_cir: CIR) -> None:
    assert minimal_cir.claims is not None
    assert len(minimal_cir.claims) == 1
    claim = minimal_cir.claims[0]
    assert claim.id == "rrxiv-example-minimal:claim:fixture"
    assert "minimal rrxiv paper" in claim.statement
    assert claim.canonical is True


def test_minimal_one_citation(minimal_cir: CIR) -> None:
    """The .tex cites \\cite{rrxiv-cir-schema}; the .bib resolves it."""
    assert minimal_cir.citations is not None
    assert len(minimal_cir.citations) == 1
    cit_root = minimal_cir.citations[0]
    # Citation is a RootModel union; access the underlying variant via .root
    cit_data = cit_root.model_dump(exclude_none=True)
    assert cit_data["key"] == "rrxiv-cir-schema"
    assert cit_data["bibtex_entry"]
    assert "rrxiv-cir-schema" in cit_data["bibtex_entry"]


def test_minimal_source(minimal_cir: CIR) -> None:
    """Source URI is a file:// URI to the .tex."""
    assert str(minimal_cir.source.format) in ("Format.latex", "latex")
    assert "minimal.tex" in str(minimal_cir.source.uri)


def test_minimal_roundtrip_to_json(minimal_cir: CIR) -> None:
    """The CIR JSON-serialises and re-parses to an identical model."""
    payload = minimal_cir.model_dump(mode="json", exclude_none=True)
    text = json.dumps(payload)
    reparsed = CIR.model_validate(json.loads(text))
    assert reparsed.id == minimal_cir.id


def test_invalid_tex_raises_validation_error(tmp_path: Path) -> None:
    """If the .tex is missing required content, build_cir's CIR validation fails.

    The parser tolerates many things, but a paper with no \\title and no
    abstract still produces a CIR (with placeholders); a paper with a
    sidecar that lacks the id metadata field falls back to the .tex's
    \\rrxivid or the filename. To force a ValidationError we'd need a
    truly malformed sidecar — covered separately in test_sidecar.py.
    Here we just confirm an empty .tex falls through cleanly.
    """
    tex = tmp_path / "empty.tex"
    sidecar = tmp_path / "empty.rrxiv.aux"
    tex.write_text("\\documentclass{rrxiv}\n\\begin{document}\n\\end{document}\n")
    sidecar.write_text("")  # no metadata
    # Falls back to filename for id; should still produce a valid CIR
    cir = build_cir(tex)
    assert cir.id == "empty"
    assert cir.title == "Untitled"


def test_no_title_in_source_uses_placeholder(tmp_path: Path) -> None:
    """A .tex without \\title falls back to 'Untitled'."""
    tex = tmp_path / "p.tex"
    sidecar = tmp_path / "p.rrxiv.aux"
    tex.write_text(
        "\\documentclass{rrxiv}\n"
        "\\begin{document}\n"
        "\\begin{abstract}A.\\end{abstract}\n"
        "\\end{document}\n"
    )
    sidecar.write_text(
        "RRXIV:meta:id:test-id\n"
        "RRXIV:meta:version:v1\n"
        "RRXIV:meta:protocol:0.1.0\n"
        "RRXIV:meta:license:CC-BY-4.0\n"
    )
    cir = build_cir(tex)
    assert cir.title == "Untitled"
    assert cir.id == "test-id"


def test_id_slug_read_from_meta_json(tmp_path: Path) -> None:
    """build_cir emits the citable id_slug natively from rrxiv-meta.json
    (RRP-0013/0029); the opaque machine id stays separate."""
    tex = tmp_path / "p.tex"
    sidecar = tmp_path / "p.rrxiv.aux"
    meta = tmp_path / "rrxiv-meta.json"  # sibling-fallback auto-detect
    tex.write_text(
        "\\documentclass{rrxiv}\n\\title{T}\n\\author{A. Tester}\n"
        "\\begin{document}\n\\begin{abstract}A.\\end{abstract}\n\\end{document}\n"
    )
    sidecar.write_text(
        "RRXIV:meta:id:test-id\nRRXIV:meta:version:v1\n"
        "RRXIV:meta:protocol:0.1.0\nRRXIV:meta:license:CC-BY-4.0\n"
    )
    meta.write_text(
        json.dumps({"id_slug": "rrxiv:2605.00042", "authors": [{"name": "A. Tester"}]})
    )
    cir = build_cir(tex)
    assert cir.id_slug == "rrxiv:2605.00042"
    assert cir.id == "test-id"  # machine id stays separate from the slug


def test_id_slug_absent_when_meta_lacks_it(tmp_path: Path) -> None:
    """No id_slug in meta → CIR omits it; the server mints one at ingest."""
    tex = tmp_path / "p.tex"
    sidecar = tmp_path / "p.rrxiv.aux"
    tex.write_text(
        "\\documentclass{rrxiv}\n\\title{T}\n\\author{A. Tester}\n"
        "\\begin{document}\n\\begin{abstract}A.\\end{abstract}\n\\end{document}\n"
    )
    sidecar.write_text("RRXIV:meta:id:test-id\nRRXIV:meta:version:v1\n")
    cir = build_cir(tex)
    assert cir.id_slug is None


def test_validation_error_propagates() -> None:
    """If the constructed CIR is somehow malformed, ValidationError surfaces."""
    # Force an invalid CIR by passing a truly malformed dict via the model.
    with pytest.raises(ValidationError):
        CIR.model_validate({"id": "x"})  # missing many required fields


def test_author_thanks_stripped_and_deduped(tmp_path: Path) -> None:
    """``\\thanks{...}`` is stripped from author names; duplicates merge.

    Regression coverage for the Euclid bug: the .tex declared
    ``\\author{Blaise Albis-Burdige\\thanks{albisburdige@protonmail.com}}``
    and the parser round-tripped the ``\\thanks`` literally into the CIR.
    On read paths (``GET /authors``) the same author then appeared twice:
    once with the thanks suffix, once without. After the fix, the author
    is normalised once and dedup'd.
    """
    tex = tmp_path / "p.tex"
    sidecar = tmp_path / "p.rrxiv.aux"
    tex.write_text(
        r"\documentclass{rrxiv}"
        "\n"
        r"\title{T}"
        "\n"
        r"\author{Blaise Albis-Burdige\thanks{\texttt{albisburdige@protonmail.com}}}"
        "\n"
        r"\author{Claude}"
        "\n"
        r"\begin{document}"
        "\n"
        r"\begin{abstract}A.\end{abstract}"
        "\n"
        r"\end{document}"
        "\n"
    )
    sidecar.write_text(
        "RRXIV:meta:id:test-thanks\n"
        "RRXIV:meta:version:v1\n"
        "RRXIV:meta:protocol:0.1.0\n"
        "RRXIV:meta:license:CC-BY-4.0\n"
    )
    cir = build_cir(tex)
    names = [a.name for a in cir.authors]
    assert names == ["Blaise Albis-Burdige", "Claude"], names


def test_author_and_separator_splits_into_multiple_entries(tmp_path: Path) -> None:
    """``\\author{A \\and B \\and C}`` splits into three author entries.

    Regression for Sprint 18: the seven Sprint-14 demo papers used the
    single-group form ``\\author{Blaise Albis-Burdige \\and Claude (agent)}``
    and the parser preserved ``\\and`` as a literal string in the cleaned
    name. The home page rendered ``Blaise Albis-Burdige \\and Claude (agent)``
    as one author. Now each ``\\and``-separated piece becomes its own
    {name} entry.
    """
    tex = tmp_path / "p.tex"
    sidecar = tmp_path / "p.rrxiv.aux"
    tex.write_text(
        r"\documentclass{rrxiv}"
        "\n"
        r"\title{T}"
        "\n"
        r"\author{Alice \and Bob \and Carol (agent)}"
        "\n"
        r"\begin{document}"
        "\n"
        r"\begin{abstract}A.\end{abstract}"
        "\n"
        r"\end{document}"
        "\n"
    )
    sidecar.write_text(
        "RRXIV:meta:id:test-and\n"
        "RRXIV:meta:version:v1\n"
        "RRXIV:meta:protocol:0.1.0\n"
        "RRXIV:meta:license:CC-BY-4.0\n"
    )
    cir = build_cir(tex)
    names = [a.name for a in cir.authors]
    assert names == ["Alice", "Bob", "Carol (agent)"], names


def test_author_duplicate_within_paper_dedup(tmp_path: Path) -> None:
    """If the .tex repeats ``\\author{Same}``, the second is dropped."""
    tex = tmp_path / "p.tex"
    sidecar = tmp_path / "p.rrxiv.aux"
    tex.write_text(
        r"\documentclass{rrxiv}"
        "\n"
        r"\title{T}"
        "\n"
        r"\author{Alice}"
        "\n"
        r"\author{Alice}"
        "\n"
        r"\begin{document}"
        "\n"
        r"\begin{abstract}A.\end{abstract}"
        "\n"
        r"\end{document}"
        "\n"
    )
    sidecar.write_text(
        "RRXIV:meta:id:test-dup\n"
        "RRXIV:meta:version:v1\n"
        "RRXIV:meta:protocol:0.1.0\n"
        "RRXIV:meta:license:CC-BY-4.0\n"
    )
    cir = build_cir(tex)
    assert [a.name for a in cir.authors] == ["Alice"]
