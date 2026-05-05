"""Annotation-related utilities.

The base :class:`rrxiv.models.Annotation` validates the wire-shape of any
annotation (target, type, content, provenance, timestamps). It does not
validate the kind-specific ``structured_payload`` — the schema declares
it as a free-form object/null.

This subpackage layers pydantic validators on top, so a caller can
assert that, e.g., a ``replication`` annotation's payload contains the
fields ``spec/0006-annotations.md`` expects (``outcome``, ``method``,
``n``, …).

Public API:

>>> from rrxiv.annotations import (
...     load_annotation,
...     load_annotations,
...     validate_annotation_payload,
... )
"""

from rrxiv.annotations.load import (
    load_annotation,
    load_annotations,
    load_annotations_file,
)
from rrxiv.annotations.payloads import (
    PAYLOAD_MODELS,
    AnnotationPayloadError,
    validate_annotation_payload,
)

__all__ = [
    "PAYLOAD_MODELS",
    "AnnotationPayloadError",
    "load_annotation",
    "load_annotations",
    "load_annotations_file",
    "validate_annotation_payload",
]
