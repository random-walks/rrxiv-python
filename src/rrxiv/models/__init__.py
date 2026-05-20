"""Pydantic v2 models for the rrxiv protocol.

Generated from the JSON Schemas in ``rrxiv/schema/`` via
``scripts/regen_models.sh``. Do not edit modules under ``_generated/`` by
hand — run the regen script.

Public surface::

    from rrxiv.models import Paper, Claim, Annotation, Citation, CIR

The Pydantic v2 models accept either dict-style construction or keyword
arguments, and validate against the schema on construction.
"""

from rrxiv.models._generated.annotation_schema import (
    Annotation,
    AnnotationType,
    CreatedBy,
    IdentityType,
    TargetType,
)
from rrxiv.models._generated.cir_schema import (
    CanonicalIntermediateRepresentationCir as CIR,  # noqa: N814 — CIR is an acronym, not a constant
)
from rrxiv.models._generated.citation_schema import Citation
from rrxiv.models._generated.claim_schema import (
    Claim,
    ClaimType,
    Confidence,
    EvidenceType,
    ExtractedBy,
    ReplicationStatus,
    Scope,
    SourceLocation,
)
from rrxiv.models._generated.figure_schema import Figure
from rrxiv.models._generated.paper_list_item_schema import (
    PaperListItem,
    Stats,
)
from rrxiv.models._generated.paper_list_item_schema import (
    Status as PaperStatus,
)
from rrxiv.models._generated.paper_schema import (
    Author,
    Format,
    Paper,
    Source,
)
from rrxiv.models._generated.section_schema import Section

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
    "PaperListItem",
    "PaperStatus",
    "ReplicationStatus",
    "Scope",
    "Section",
    "Source",
    "SourceLocation",
    "Stats",
    "TargetType",
]
