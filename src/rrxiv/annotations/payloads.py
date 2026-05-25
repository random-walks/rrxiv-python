"""Pydantic models for the per-type ``structured_payload`` of each
annotation kind, per ``spec/0006-annotations.md``.

The base :class:`rrxiv.models.Annotation` schema leaves
``structured_payload`` as a free-form object/null. This module narrows
that for each ``annotation_type`` so callers can validate the payload
shape and surface specific errors.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rrxiv.models import Annotation


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


class RevisionSummaryPayload(_PayloadBase):
    """Per spec/0006 §revision_summary, refined in Sprint 19.

    Attached to the *newer* paper in a previous_version chain. Servers
    may synthesise a skeleton from the submission's revision_summary
    form field (RRP-0017); authors can supersede with a richer entry
    containing per-claim highlights.
    """

    previous_version_id: str
    summary: str
    highlights: list[dict[str, Any]] | None = None


# ---- Retraction-family payloads (RRP-0020 + Sprint 18 paper_retraction) ----

# Sprint 19 — settles the retraction-reason taxonomy from
# rrxiv:2605.00007 c4 ("Five reason categories cover 94% of historical
# retractions") plus `superseded_by_revision` for the revision-of case.
# Both claim and paper retraction share this enum so dedup is easy.

RetractionReason = Literal[
    "data_error",
    "methodological_flaw",
    "fraud",
    "contamination",
    "withdrawn_by_author",
    "superseded_by_revision",
]

# Sprint 19 — recommended_action lets the server give a downstream reader
# a one-line "what should I do with this now?" without re-deriving from
# the reason. Optional.
RetractionRecommendedAction = Literal[
    "use_v2",
    "file_v2",
    "no_action",
    "see_replications",
    "use_superseded_by",
]


class _RetractionPayloadBase(_PayloadBase):
    """Shared shape for claim and paper retractions.

    The two types differ only in what their parent annotation's
    `target_id` points at (a claim vs a paper). The payload itself is
    identical so models that ingest annotations can reuse the same
    parser.
    """

    reason: RetractionReason
    explanation: str | None = Field(
        default=None,
        description=(
            "Plain-text explanation. Optional in v0.1 — the `reason` enum "
            "carries the load-bearing semantics — but encouraged for "
            "non-obvious cases like contamination or methodological_flaw."
        ),
    )
    superseded_by_paper: str | None = None
    superseded_by_claim: str | None = None
    recommended_action: RetractionRecommendedAction | None = None


class ClaimRetractionPayload(_RetractionPayloadBase):
    """RRP-0020 — author-only fast-path retraction of a single claim.

    The annotation's `target_type` must be `claim`; the server enforces.
    A non-superseded, non-lifted retraction overrides the derived
    `replication_status` of the target to `retracted`. A lift is a
    later `comment` annotation by the same identity with
    `in_reply_to` pointing at the retraction and
    `structured_payload.lifts_retraction: true`.
    """


class PaperRetractionPayload(_RetractionPayloadBase):
    """Sprint 18 — paper-level sibling of claim_retraction. Motivated
    by rrxiv:2605.00007 c1 ("Retraction is more naturally modelled as
    an annotation type"). When set, downstream readers should treat the
    entire paper as retracted, but the v0.1 server does not currently
    propagate this to per-claim status (each claim is retracted
    independently via claim_retraction). Future RRP territory."""


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
    "revision_summary": RevisionSummaryPayload,
    "claim_retraction": ClaimRetractionPayload,
    "paper_retraction": PaperRetractionPayload,
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
