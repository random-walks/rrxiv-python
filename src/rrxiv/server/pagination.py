"""Cursor pagination helpers (RRP-0014).

Opaque base64url-encoded JSON cursors carrying a keyset position. The
v0.1 reference server materialises full result sets in memory and slices
— sufficient for ≤10k items. Postgres/Tantivy backends will swap this
for proper keyset queries without changing the wire format.

Usage from a router::

    from rrxiv.server.pagination import paginate

    page, next_cursor = paginate(
        items,
        cursor=cursor_param,
        limit=limit_param,
        key=lambda item: (item["submitted_at"], item["id"]),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Sequence

from rrxiv.server.errors import bad_request

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode a cursor payload to base64url-safe text."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(token: str) -> dict[str, Any]:
    """Decode a cursor token back to its payload.

    Raises ``bad_request`` ("invalid_cursor") on malformed input — callers
    propagate so the FastAPI app returns 400.
    """
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise bad_request(
            "Cursor could not be decoded.",
            extra={"code": "invalid_cursor"},
        ) from exc
    if not isinstance(payload, dict):
        raise bad_request(
            "Cursor payload must be an object.",
            extra={"code": "invalid_cursor"},
        )
    return payload


def clamp_limit(limit: int | None) -> int:
    """Validate and clamp the requested page size."""
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise bad_request("limit must be >= 1", extra={"code": "invalid_limit"})
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def paginate(
    items: Sequence[dict[str, Any]],
    *,
    cursor: str | None,
    limit: int | None,
    key: Callable[[dict[str, Any]], tuple[Any, ...]],
    order: str = "desc",
) -> tuple[list[dict[str, Any]], str | None]:
    """Slice ``items`` to the page identified by ``cursor`` / ``limit``.

    ``key`` extracts the keyset tuple used for ordering and cursor
    placement. ``order`` is ``"desc"`` or ``"asc"``.

    The result is ``(page_items, next_cursor)``; ``next_cursor`` is
    ``None`` when no more pages remain.
    """
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be asc or desc, got {order!r}")
    page_size = clamp_limit(limit)

    sorted_items = sorted(items, key=key, reverse=(order == "desc"))

    start = 0
    if cursor:
        payload = decode_cursor(cursor)
        # Cursor's keyset must match the key function — payload may carry
        # extra fields the caller doesn't use; we only consume what the
        # key returns. Match against the encoded tuple under the agreed
        # field name "k".
        if "k" not in payload:
            raise bad_request(
                "Cursor is incompatible with the requested sort order.",
                extra={"code": "invalid_cursor"},
            )
        anchor = tuple(payload["k"])
        for i, item in enumerate(sorted_items):
            current = key(item)
            if _strictly_after(current, anchor, order):
                start = i
                break
        else:
            start = len(sorted_items)

    end = start + page_size
    page = list(sorted_items[start:end])
    next_cursor: str | None = None
    if end < len(sorted_items) and page:
        last_key = list(key(page[-1]))
        next_cursor = encode_cursor({"k": last_key})
    return page, next_cursor


def _strictly_after(
    candidate: tuple[Any, ...],
    anchor: tuple[Any, ...],
    order: str,
) -> bool:
    """True when ``candidate`` comes strictly after ``anchor`` under ``order``.

    Tuple comparison uses lexicographic order; mixed None/str/number tuples
    fall back to comparing string representations to remain total.
    """
    try:
        if order == "desc":
            return candidate < anchor
        return candidate > anchor
    except TypeError:
        # Non-comparable mix (e.g. None and string). Compare by repr to
        # stay deterministic.
        cand_repr = tuple(_safe_key(x) for x in candidate)
        anch_repr = tuple(_safe_key(x) for x in anchor)
        if order == "desc":
            return cand_repr < anch_repr
        return cand_repr > anch_repr


def _safe_key(value: Any) -> str:
    return "" if value is None else repr(value)
