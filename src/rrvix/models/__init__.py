"""Pydantic v2 models for the rrvix protocol.

Generated from the JSON Schemas in ``rrvix/schema/`` via
``scripts/regen_models.sh``. Do not edit modules under ``_generated/`` by
hand — run the regen script.

Public surface::

    from rrvix.models import Paper, Claim, Annotation, Citation, CIR

The Pydantic v2 models accept either dict-style construction or keyword
arguments, and validate against the schema on construction.
"""

from rrvix.models._generated.annotation_schema import (
    Annotation,
    AnnotationType,
    CreatedBy,
    IdentityType,
    TargetType,
)
from rrvix.models._generated.cir_schema import (
    CanonicalIntermediateRepresentationCir as CIR,  # noqa: N814 — CIR is an acronym, not a constant
)
from rrvix.models._generated.cir_schema import (
    Figure,
    Section,
)
from rrvix.models._generated.citation_schema import Citation
from rrvix.models._generated.claim_schema import (
    Claim,
    ClaimType,
    Confidence,
    EvidenceType,
    ExtractedBy,
    ReplicationStatus,
    Scope,
    SourceLocation,
)
from rrvix.models._generated.paper_schema import (
    Author,
    Format,
    Paper,
    Source,
)

__all__ = [
    "CIR",
    "Annotation",
    "AnnotationType",
    "Author",
    "Citation",
    "Claim",
    "ClaimType",
    "Confidence",
    "CreatedBy",
    "EvidenceType",
    "ExtractedBy",
    "Figure",
    "Format",
    "IdentityType",
    "Paper",
    "ReplicationStatus",
    "Scope",
    "Section",
    "Source",
    "SourceLocation",
    "TargetType",
]
