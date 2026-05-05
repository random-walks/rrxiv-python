"""Tests for the annotations module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rrxiv.annotations import (
    AnnotationPayloadError,
    load_annotation,
    load_annotations,
    load_annotations_file,
    validate_annotation_payload,
)


def _base_annotation(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "ann-1",
        "target_id": "p1:c1",
        "target_type": "claim",
        "annotation_type": "comment",
        "content": "This is a comment.",
        "created_at": "2026-06-01T08:30:00Z",
        "created_by": {"identity_type": "anonymous", "identity": "x"},
    }
    base.update(overrides)
    return base


class TestLoading:
    def test_load_one(self) -> None:
        ann = load_annotation(_base_annotation())
        assert ann.id == "ann-1"

    def test_load_many(self) -> None:
        anns = load_annotations(
            [
                _base_annotation(id="a"),
                _base_annotation(id="b"),
            ]
        )
        assert [a.id for a in anns] == ["a", "b"]

    def test_load_file_object(self, tmp_path: Path) -> None:
        p = tmp_path / "a.json"
        p.write_text(json.dumps(_base_annotation(id="from-file")))
        anns = load_annotations_file(p)
        assert len(anns) == 1
        assert anns[0].id == "from-file"

    def test_load_file_array(self, tmp_path: Path) -> None:
        p = tmp_path / "a.json"
        p.write_text(
            json.dumps([_base_annotation(id="x"), _base_annotation(id="y")])
        )
        anns = load_annotations_file(p)
        assert [a.id for a in anns] == ["x", "y"]

    def test_load_file_bad_shape(self, tmp_path: Path) -> None:
        p = tmp_path / "a.json"
        p.write_text(json.dumps(42))  # not object or array
        with pytest.raises(ValueError, match="object or array"):
            load_annotations_file(p)


class TestPayloadValidation:
    def test_comment_no_payload_passes(self) -> None:
        ann = load_annotation(_base_annotation())
        validate_annotation_payload(ann)  # no raise

    def test_comment_with_payload_fails(self) -> None:
        ann = load_annotation(
            _base_annotation(structured_payload={"unexpected": True})
        )
        with pytest.raises(AnnotationPayloadError, match="comment"):
            validate_annotation_payload(ann)

    def test_replication_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="replication",
                structured_payload={
                    "outcome": "supports",
                    "method": "computational",
                    "n": 10000,
                },
            )
        )
        validate_annotation_payload(ann)

    def test_replication_bad_outcome(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="replication",
                structured_payload={"outcome": "vibes", "method": "computational"},
            )
        )
        with pytest.raises(AnnotationPayloadError, match="replication"):
            validate_annotation_payload(ann)

    def test_replication_missing_payload(self) -> None:
        ann = load_annotation(_base_annotation(annotation_type="replication"))
        with pytest.raises(
            AnnotationPayloadError, match="requires a structured_payload"
        ):
            validate_annotation_payload(ann)

    def test_extension_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="extension",
                structured_payload={
                    "kind": "generalisation",
                    "description": "Wider regime.",
                },
            )
        )
        validate_annotation_payload(ann)

    def test_extension_bad_kind(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="extension",
                structured_payload={"kind": "weird", "description": "."},
            )
        )
        with pytest.raises(AnnotationPayloadError, match="extension"):
            validate_annotation_payload(ann)

    def test_erratum_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="erratum",
                structured_payload={
                    "error_description": "typo",
                    "corrected_statement": "fixed",
                    "scope": "claim",
                    "severity": "minor",
                },
            )
        )
        validate_annotation_payload(ann)

    def test_summary_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="summary",
                structured_payload={
                    "audience": "agent",
                    "length_words": 80,
                    "key_points": ["First", "Second"],
                },
            )
        )
        validate_annotation_payload(ann)

    def test_code_link_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="code_link",
                structured_payload={
                    "uri": "https://github.com/x/y",
                    "ref": "abc123",
                    "role": "implements",
                    "language": "Python",
                    "license": "MIT",
                },
            )
        )
        validate_annotation_payload(ann)

    def test_dataset_link_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="dataset_link",
                structured_payload={
                    "uri": "https://example.org/data.tar.gz",
                    "checksum": "sha256:abc",
                    "format": "application/x-tar",
                    "size_bytes": 12345,
                    "license": "CC-BY-4.0",
                    "role": "input",
                },
            )
        )
        validate_annotation_payload(ann)

    def test_claim_extraction_valid(self) -> None:
        ann = load_annotation(
            _base_annotation(
                annotation_type="claim_extraction",
                structured_payload={
                    "proposed_claim": {
                        "id": "p1:c-new",
                        "statement": "X under Y.",
                        "claim_type": "theoretical",
                        "evidence_type": "argument",
                    }
                },
            )
        )
        validate_annotation_payload(ann)

    def test_extra_payload_field_rejected(self) -> None:
        """Per-type payload models forbid extra fields by default."""
        ann = load_annotation(
            _base_annotation(
                annotation_type="replication",
                structured_payload={
                    "outcome": "supports",
                    "method": "computational",
                    "rogue_field": True,
                },
            )
        )
        with pytest.raises(AnnotationPayloadError, match=r"rogue_field|extra"):
            validate_annotation_payload(ann)
