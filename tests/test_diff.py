"""Tests for the CIR diff module."""

from __future__ import annotations

from typing import Any

from rrxiv.diff import diff_cir
from rrxiv.models import CIR


def _cir(claims: list[dict[str, Any]] | None = None, **overrides: Any) -> CIR:
    base: dict[str, Any] = {
        "rrxiv_version": "0.1.0",
        "id": "p1",
        "version": "v1",
        "title": "T",
        "authors": [{"name": "A. Author"}],
        "abstract": "x",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
    }
    if claims is not None:
        base["claims"] = claims
    base.update(overrides)
    return CIR.model_validate(base)


def _claim(cid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": cid,
        "statement": "X.",
        "claim_type": "theoretical",
        "evidence_type": "argument",
    }
    base.update(overrides)
    return base


def test_no_changes() -> None:
    a = _cir()
    b = _cir()
    d = diff_cir(a, b)
    assert d.is_empty()
    assert d.summary() == "No changes."


def test_title_change() -> None:
    a = _cir(title="Old title")
    b = _cir(title="New title")
    d = diff_cir(a, b)
    assert len(d.field_changes) == 1
    assert d.field_changes[0].field == "title"
    assert d.field_changes[0].before == "Old title"
    assert d.field_changes[0].after == "New title"


def test_topics_added_and_removed() -> None:
    a = _cir(topics=["a", "b"])
    b = _cir(topics=["b", "c"])
    d = diff_cir(a, b)
    assert d.topic_added == ["c"]
    assert d.topic_removed == ["a"]


def test_claim_added() -> None:
    a = _cir(claims=[_claim("p1:c1")])
    b = _cir(claims=[_claim("p1:c1"), _claim("p1:c2")])
    d = diff_cir(a, b)
    assert d.claim_added == ["p1:c2"]
    assert d.claim_removed == []
    assert d.claim_changed == []


def test_claim_removed() -> None:
    a = _cir(claims=[_claim("p1:c1"), _claim("p1:c2")])
    b = _cir(claims=[_claim("p1:c1")])
    d = diff_cir(a, b)
    assert d.claim_removed == ["p1:c2"]


def test_claim_statement_changed() -> None:
    a = _cir(claims=[_claim("p1:c1", statement="Old statement.")])
    b = _cir(claims=[_claim("p1:c1", statement="New statement.")])
    d = diff_cir(a, b)
    assert d.claim_added == []
    assert d.claim_removed == []
    assert len(d.claim_changed) == 1
    assert d.claim_changed[0].claim_id == "p1:c1"
    field_names = [fc.field for fc in d.claim_changed[0].fields]
    assert "statement" in field_names


def test_edge_added() -> None:
    a = _cir(claims=[_claim("p1:c1"), _claim("p1:c2")])
    b = _cir(
        claims=[
            _claim("p1:c1"),
            _claim("p1:c2", depends_on=["p1:c1"]),
        ]
    )
    d = diff_cir(a, b)
    assert d.edge_added == [("p1:c2", "p1:c1", "depends_on")]
    assert d.edge_removed == []


def test_edge_removed() -> None:
    a = _cir(
        claims=[
            _claim("p1:c1"),
            _claim("p1:c2", depends_on=["p1:c1"]),
        ]
    )
    b = _cir(claims=[_claim("p1:c1"), _claim("p1:c2")])
    d = diff_cir(a, b)
    assert d.edge_removed == [("p1:c2", "p1:c1", "depends_on")]


def test_to_dict_serialisable() -> None:
    a = _cir(claims=[_claim("p1:c1")])
    b = _cir(claims=[_claim("p1:c2")])
    d = diff_cir(a, b)
    payload = d.to_dict()
    assert payload["claim_added"] == ["p1:c2"]
    assert payload["claim_removed"] == ["p1:c1"]


def test_summary_lists_changes() -> None:
    a = _cir(title="A", claims=[_claim("p1:c1")])
    b = _cir(title="B", claims=[_claim("p1:c2")])
    d = diff_cir(a, b)
    text = d.summary()
    assert "title" in text
    assert "p1:c1" in text
    assert "p1:c2" in text


def test_submitted_at_ignored() -> None:
    """submitted_at differs trivially across revisions; not a change."""
    a = _cir(submitted_at="2026-01-01T00:00:00Z")
    b = _cir(submitted_at="2026-09-01T00:00:00Z")
    d = diff_cir(a, b)
    assert d.is_empty()


def test_revision_field_change_surfaced() -> None:
    """version + previous_version are part of the diff (revision tracking)."""
    a = _cir(version="v1")
    b = _cir(version="v2", previous_version="p1-v1")
    d = diff_cir(a, b)
    assert any(fc.field == "version" for fc in d.field_changes)
    assert any(fc.field == "previous_version" for fc in d.field_changes)
