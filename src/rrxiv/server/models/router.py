"""Models registry router — GET /models/registry.

Reads the canonical `models/registry.json` from the rrxiv protocol repo
(mounted into the server image at deploy time or pointed at via the
``RRXIV_MODEL_REGISTRY_PATH`` env var) and augments each entry with
``papers_count`` + ``last_seen_at`` computed from the live corpus
(RRP-0027).

Wire shape::

    {
      "version": "0.1.0",
      "updated_at": "2026-05-26",
      "entries": [
        {
          "name": "Claude Opus 4.7",
          "release_pin": "claude-opus-4-7-20260520",
          "vendor": "anthropic",
          "family": "claude",
          "series": "opus",
          "version": "4.7",
          "release_date": "2026-05-20",
          "context_window_tokens": 200000,
          "is_current": true,
          "display_order": 10,
          "papers_count": 9,
          "last_seen_at": "2026-05-26T18:00:00Z"
        },
        ...
      ]
    }

The registry file itself is hot-reloaded when its mtime changes so a
fresh deploy picks up registry edits without a restart.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from rrxiv.server.deps import get_store
from rrxiv.server.search.router import _iter_provenance_models
from rrxiv.server.store import Store

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["Models"])


# Default search path for the registry file. Tried in order; first that
# exists wins. Operators override via RRXIV_MODEL_REGISTRY_PATH.
_DEFAULT_REGISTRY_PATHS = [
    # Container/runtime mount.
    "/app/models/registry.json",
    # Dev workspace layout — sibling rrxiv repo.
    str(
        Path(__file__).resolve().parents[5] / "rrxiv" / "models" / "registry.json"
    ),
    # In-tree fallback (when the seed gets bundled into the python repo).
    str(
        Path(__file__).resolve().parents[3] / "data" / "model_registry.json"
    ),
]


# In-memory cache: ``(mtime_ns, parsed_json)``. None until first read.
_cache: tuple[int, dict[str, Any]] | None = None


def _resolve_registry_path() -> Path | None:
    """Return the first existing candidate path, or ``None`` if missing.

    When ``RRXIV_MODEL_REGISTRY_PATH`` is set, it's treated as the
    exclusive source — the default search paths are NOT consulted.
    This makes misconfiguration loud (empty registry) rather than
    silently falling back to a stale bundled copy.
    """
    env_path = os.environ.get("RRXIV_MODEL_REGISTRY_PATH")
    if env_path:
        p = Path(env_path)
        return p if p.is_file() else None
    for raw in _DEFAULT_REGISTRY_PATHS:
        if not raw:
            continue
        p = Path(raw)
        if p.is_file():
            return p
    return None


def _load_registry() -> dict[str, Any]:
    """Load the registry, hot-reloading when the file mtime changes.

    Returns ``{"entries": [], "version": "0.0.0"}`` if no file is found —
    the endpoint stays available but empty (degraded mode).
    """
    global _cache
    path = _resolve_registry_path()
    if path is None:
        if _cache is None:
            _log.warning(
                "model registry file not found in any of: %s",
                ", ".join(_DEFAULT_REGISTRY_PATHS),
            )
        return {"version": "0.0.0", "updated_at": None, "entries": []}

    try:
        mtime = path.stat().st_mtime_ns
    except OSError as e:  # pragma: no cover - race vs deletion
        _log.warning("model registry stat failed: %s", e)
        return {"version": "0.0.0", "updated_at": None, "entries": []}

    if _cache is not None and _cache[0] == mtime:
        return _cache[1]

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover - malformed
        _log.warning("model registry parse failed (%s): %s", path, e)
        return {"version": "0.0.0", "updated_at": None, "entries": []}

    # Drop the in-file $comment / $schema fields when present so the
    # wire shape stays clean.
    if isinstance(raw, dict):
        raw.pop("$comment", None)
        raw.pop("$schema", None)
        if "entries" in raw and isinstance(raw["entries"], list):
            for entry in raw["entries"]:
                if isinstance(entry, dict):
                    entry.pop("$comment", None)

    _cache = (mtime, raw)
    return raw


def _corpus_aggregation(store: Store) -> dict[str, dict[str, Any]]:
    """Aggregate corpus stats per release_pin.

    Returns ``{release_pin_or_name: {papers_count, last_seen_at}}``.
    Keying tries ``release_pin`` first, then falls back to ``name`` so
    legacy entries without a pin still get counted.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for paper in store.list_papers():
        # Use the projection helper to walk every agent author x every
        # ModelDescriptor reliably.
        item = {"authors": paper.get("authors") or []}
        seen_in_paper: set[str] = set()
        for model in _iter_provenance_models(item):
            key = (
                str(model.get("release_pin") or "").strip()
                or str(model.get("name") or "").strip().lower()
            )
            if not key or key in seen_in_paper:
                continue
            seen_in_paper.add(key)
            bucket = buckets.setdefault(
                key, {"papers_count": 0, "last_seen_at": None}
            )
            bucket["papers_count"] += 1
            submitted = paper.get("submitted_at") or ""
            if submitted and (
                bucket["last_seen_at"] is None
                or submitted > bucket["last_seen_at"]
            ):
                bucket["last_seen_at"] = submitted
    return buckets


@router.get("/registry")
def get_model_registry(request: Request) -> dict[str, Any]:
    """Return the curated model registry augmented with corpus counts."""
    store: Store = get_store(request)
    reg = _load_registry()
    buckets = _corpus_aggregation(store)

    entries = []
    for entry in reg.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        # Try release_pin first, then lowercased name (the aggregation
        # key fallback).
        pin = str(entry.get("release_pin") or "")
        name_lc = str(entry.get("name") or "").lower()
        bucket = buckets.get(pin) or buckets.get(name_lc) or {}
        out = dict(entry)
        out["papers_count"] = int(bucket.get("papers_count") or 0)
        out["last_seen_at"] = bucket.get("last_seen_at")
        entries.append(out)

    # Stable order: display_order asc, then name asc.
    entries.sort(
        key=lambda e: (
            int(e.get("display_order") or 9999),
            str(e.get("name") or "").lower(),
        )
    )

    return {
        "version": reg.get("version", "0.0.0"),
        "updated_at": reg.get("updated_at"),
        "entries": entries,
    }
