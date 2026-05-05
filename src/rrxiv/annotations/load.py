"""Loading annotations from JSON files.

Three loaders cover the common cases:

- :func:`load_annotation` — one annotation JSON dict → :class:`Annotation`.
- :func:`load_annotations` — a JSON list of dicts → list of Annotations.
- :func:`load_annotations_file` — read either shape from disk; the loader
  detects whether the JSON is an object (single annotation) or array
  (multiple).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rrxiv.models import Annotation


def load_annotation(data: dict[str, Any]) -> Annotation:
    """Construct an Annotation from a dict. Raises pydantic
    ``ValidationError`` if the dict doesn't match the schema."""
    return Annotation.model_validate(data)


def load_annotations(data: list[dict[str, Any]]) -> list[Annotation]:
    """Construct a list of Annotations from a JSON array of dicts."""
    return [Annotation.model_validate(d) for d in data]


def load_annotations_file(path: Path | str) -> list[Annotation]:
    """Read annotations from a JSON file on disk.

    Accepts either:

    - A single JSON object (one annotation) → list of length 1.
    - A JSON array of objects → list with that many entries.
    """
    text = Path(path).read_text(encoding="utf-8")
    obj = json.loads(text)
    if isinstance(obj, dict):
        return [load_annotation(obj)]
    if isinstance(obj, list):
        return load_annotations(obj)
    raise ValueError(
        f"{path}: expected JSON object or array, got {type(obj).__name__}"
    )
