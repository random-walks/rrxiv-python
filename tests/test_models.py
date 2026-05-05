"""Pydantic model tests.

Construct each generated model with minimal kwargs, run a round-trip
(model -> dict -> model), and verify the dict is JSON-serializable.

Also: load every fixture from the rrxiv repo's ``tests/schemas/fixtures/``
(if available), validate each one through the corresponding pydantic
model, and check the verdict matches the filename convention
(``*-valid-*`` must construct, ``*-invalid-*`` must raise).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ValidationError

from rrxiv.models import (
    CIR,
    Annotation,
    Author,
    Citation,
    Claim,
    Paper,
    Source,
)


def _roundtrip(model: BaseModel) -> None:
    dumped = model.model_dump(mode="json")
    json.dumps(dumped)  # must be JSON-serializable
    type(model).model_validate(dumped)


class TestSmokeConstruction:
    """Each model can be constructed with minimal valid kwargs."""

    def test_author(self) -> None:
        a = Author(name="A. Author")
        assert a.name == "A. Author"
        _roundtrip(a)

    def test_source(self) -> None:
        s = Source(format="latex", uri="https://example.org/p.tar.gz")  # type: ignore[arg-type]
        assert str(s.format) == "Format.latex" or str(s.format) == "latex"
        _roundtrip(s)

    def test_paper_minimal(self) -> None:
        p = Paper.model_validate(
            {
                "rrxiv_version": "0.1.0",
                "id": "01923f8e-5b2a-7c4d-9e1f-3a2b1c0d4e5f",
                "version": "v1",
                "title": "T",
                "authors": [{"name": "A. Author"}],
                "abstract": "A",
                "submitted_at": "2026-05-04T12:00:00Z",
                "license": "CC-BY-4.0",
                "source": {"format": "latex", "uri": "https://example.org/p.tar.gz"},
            }
        )
        assert p.id == "01923f8e-5b2a-7c4d-9e1f-3a2b1c0d4e5f"
        _roundtrip(p)

    def test_claim_minimal(self) -> None:
        c = Claim.model_validate(
            {
                "id": "p1:c1",
                "statement": "X under Y.",
                "claim_type": "theoretical",
                "evidence_type": "argument",
            }
        )
        assert c.id == "p1:c1"
        _roundtrip(c)

    def test_annotation_minimal(self) -> None:
        a = Annotation.model_validate(
            {
                "id": "ann-1",
                "target_id": "p1:c1",
                "target_type": "claim",
                "annotation_type": "comment",
                "content": "Looks fine.",
                "created_at": "2026-06-01T00:00:00Z",
                "created_by": {"identity_type": "anonymous", "identity": "x"},
            }
        )
        assert a.id == "ann-1"
        _roundtrip(a)

    def test_citation_arxiv(self) -> None:
        # Citation is a RootModel union; construct via dict.
        c = Citation.model_validate(
            {
                "id": "cite-1",
                "key": "tao2024",
                "target_arxiv_id": "2404.12345",
                "bibtex_entry": "@misc{tao2024, year={2024}}",
            }
        )
        _roundtrip(c)

    def test_cir_minimal(self) -> None:
        cir = CIR.model_validate(
            {
                "rrxiv_version": "0.1.0",
                "id": "p1",
                "version": "v1",
                "title": "Minimal CIR",
                "authors": [{"name": "A."}],
                "abstract": "A.",
                "submitted_at": "2026-05-04T12:00:00Z",
                "license": "CC-BY-4.0",
                "source": {"format": "latex", "uri": "https://example.org/p.tar.gz"},
            }
        )
        assert cir.id == "p1"
        _roundtrip(cir)


# ---- Fixture-driven tests against rrxiv repo's tests/schemas/fixtures/ ----


def _find_rrxiv_fixtures_dir() -> Path | None:
    """Locate the rrxiv repo's tests/schemas/fixtures/ if it sits next to us
    in the workspace pattern (../rrxiv/...)."""
    candidates = [
        Path(__file__).resolve().parents[2] / "rrxiv" / "tests" / "schemas" / "fixtures",
        Path("/Users/blaise/Desktop/blaise-oss/rrxiv-dev-workspace/repos/rrxiv/tests/schemas/fixtures"),
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.json")):
            return c
    return None


_FIXTURES_DIR = _find_rrxiv_fixtures_dir()


_KIND_TO_MODEL: Final[dict[str, type[BaseModel]]] = {
    "paper": Paper,
    "claim": Claim,
    "annotation": Annotation,
    "citation": Citation,
    "cir": CIR,
}


def _classify(name: str) -> tuple[str, bool] | None:
    parts = name.split("-", 2)
    if len(parts) < 3:
        return None
    kind, validity = parts[0], parts[1]
    if kind not in _KIND_TO_MODEL:
        return None
    if validity not in ("valid", "invalid"):
        return None
    return kind, validity == "valid"


@pytest.mark.skipif(_FIXTURES_DIR is None, reason="rrxiv repo fixtures not available")
@pytest.mark.parametrize(
    "fixture_path",
    sorted(_FIXTURES_DIR.glob("*.json")) if _FIXTURES_DIR else [],
    ids=lambda p: p.name,
)
def test_pydantic_matches_ajv(fixture_path: Path) -> None:
    """Every rrxiv fixture must validate (or fail) the same way through
    pydantic as it does through ajv. Sanity: codegen output agrees with
    the JSON Schema it was generated from."""
    cls = _classify(fixture_path.stem)
    if cls is None:
        pytest.skip(f"unrecognised fixture name: {fixture_path.name}")
    kind, expect_valid = cls
    model = _KIND_TO_MODEL[kind]
    data: Any = json.loads(fixture_path.read_text())

    if expect_valid:
        model.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(data)
