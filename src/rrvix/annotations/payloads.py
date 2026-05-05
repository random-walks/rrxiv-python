"""Pydantic models for the per-type ``structured_payload`` of each
annotation kind, per ``spec/0006-annotations.md``.

The base :class:`rrvix.models.Annotation` schema leaves
``structured_payload`` as a free-form object/null. This module narrows
that for each ``annotation_type`` so callers can validate the payload
shape and surface specific errors.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rrvix.models import Annotation


class AnnotationPayloadError(ValueError):
    """Raised when an annotation's ``structured_payload`` doesn't match
    the schema expected for its ``annotation_type``."""

    def __init__(self, annotation_type: str, errors: str):
        super().__init__(f"{annotation_type}: {errors}")
        self.annotation_type = annotation_type
        self.errors_summary = errors


# ---- Per-type payloads ----


class _PayloadBase(BaseModel):
    """Reject extra fields by default. Encourages early discovery of
    typos and out-of-spec keys."""

    model_config = ConfigDict(extra="forbid")


class ReplicationPayload(_PayloadBase):
    """Per spec/0006 §replication."""

    outcome: Literal["supports", "contradicts", "partial", "inconclusive"]
    method: Literal["computational", "experimental", "analytical", "theoretical"]
    n: Annotated[int, Field(ge=0)] | None = None
    effect_size: dict[str, Any] | None = None
    code_uri: str | None = None
    data_uri: str | None = None


class ContradictionPayload(_PayloadBase):
    contradicting_claim_id: str | None = None
    scope_overlap: str | None = None
    reasoning: str


class ExtensionPayload(_PayloadBase):
    extending_claim_id: str | None = None
    kind: Literal["generalisation", "refinement", "strengthening"]
    description: str


class ErratumPayload(_PayloadBase):
    error_description: str
    corrected_statement: str
    scope: Literal["claim", "section", "paper"]
    severity: Literal["minor", "moderate", "major"]


class SummaryPayload(_PayloadBase):
    audience: Literal["expert", "general", "agent"]
    length_words: Annotated[int, Field(ge=1)]
    key_points: list[str] = Field(default_factory=list, min_length=1)


class CodeLinkPayload(_PayloadBase):
    uri: str
    ref: str | None = None
    role: Literal["implements", "reproduces", "extends", "tests"]
    language: str | None = None
    license: str | None = None


class DatasetLinkPayload(_PayloadBase):
    uri: str
    checksum: str | None = None
    format: str | None = None
    size_bytes: Annotated[int, Field(ge=0)] | None = None
    license: str | None = None
    role: Literal["input", "output", "validation"]


class ClaimExtractionPayload(_PayloadBase):
    """The proposed claim. We don't validate the inner Claim against
    claim.schema.json here — caller can run it through the Claim model
    separately if they want full validation."""

    proposed_claim: dict[str, Any]


# ---- Mapping ----

PAYLOAD_MODELS: dict[str, type[BaseModel] | None] = {
    "replication": ReplicationPayload,
    "contradiction": ContradictionPayload,
    "extension": ExtensionPayload,
    "erratum": ErratumPayload,
    "summary": SummaryPayload,
    "comment": None,  # comment has no structured_payload
    "code_link": CodeLinkPayload,
    "dataset_link": DatasetLinkPayload,
    "claim_extraction": ClaimExtractionPayload,
}


def validate_annotation_payload(annotation: Annotation) -> None:
    """Validate an Annotation's ``structured_payload`` against the
    per-type schema in this module.

    Raises:
        AnnotationPayloadError: if the payload is invalid for the type.
    """
    annotation_type = str(annotation.annotation_type)
    # The pydantic enum's str() returns "AnnotationType.replication"; strip
    # the prefix so we can look up by the enum value.
    if "." in annotation_type:
        annotation_type = annotation_type.rsplit(".", 1)[-1]

    if annotation_type not in PAYLOAD_MODELS:
        raise AnnotationPayloadError(
            annotation_type, f"unknown annotation_type {annotation_type!r}"
        )

    model = PAYLOAD_MODELS[annotation_type]
    payload = annotation.structured_payload

    if model is None:
        # comment: payload should be missing/null
        if payload is not None:
            raise AnnotationPayloadError(
                annotation_type,
                "annotation_type=comment must have structured_payload null/missing",
            )
        return

    if payload is None:
        raise AnnotationPayloadError(
            annotation_type,
            f"annotation_type={annotation_type} requires a structured_payload",
        )

    try:
        model.model_validate(payload)
    except ValidationError as e:
        raise AnnotationPayloadError(annotation_type, str(e)) from e
