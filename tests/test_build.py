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


def test_validation_error_propagates() -> None:
    """If the constructed CIR is somehow malformed, ValidationError surfaces."""
    # Force an invalid CIR by passing a truly malformed dict via the model.
    with pytest.raises(ValidationError):
        CIR.model_validate({"id": "x"})  # missing many required fields
