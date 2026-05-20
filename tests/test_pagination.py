"""Tests for the cursor pagination helper (RRP-0014)."""

from __future__ import annotations

import pytest

from rrxiv.server.errors import ProblemError
from rrxiv.server.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    paginate,
)


def _make_items(count: int) -> list[dict[str, object]]:
    return [
        {"id": f"id-{i:03d}", "submitted_at": f"2026-05-{(i % 28) + 1:02d}T00:00:00Z"}
        for i in range(count)
    ]


def test_encode_decode_round_trip() -> None:
    payload = {"k": ["2026-05-04T00:00:00Z", "abc-123"]}
    token = encode_cursor(payload)
    decoded = decode_cursor(token)
    assert decoded == payload


def test_decode_invalid_cursor_raises_400() -> None:
    with pytest.raises(ProblemError) as exc:
        decode_cursor("not-base64-!!!")
    assert exc.value.status == 400


def test_clamp_limit_defaults_and_max() -> None:
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit(5) == 5
    assert clamp_limit(MAX_LIMIT + 1) == MAX_LIMIT
    with pytest.raises(ProblemError):
        clamp_limit(0)


def test_paginate_first_page_returns_top_items() -> None:
    items = _make_items(10)
    page, cursor = paginate(
        items,
        cursor=None,
        limit=3,
        key=lambda x: (x["submitted_at"], x["id"]),
        order="desc",
    )
    assert len(page) == 3
    assert cursor is not None
    # Descending order: highest submitted_at first.
    assert page[0]["submitted_at"] >= page[1]["submitted_at"]


def test_paginate_walks_pages_to_completion() -> None:
    items = _make_items(7)
    visited: list[str] = []
    cursor: str | None = None
    seen_pages = 0
    while True:
        page, cursor = paginate(
            items,
            cursor=cursor,
            limit=3,
            key=lambda x: (x["submitted_at"], x["id"]),
            order="desc",
        )
        for item in page:
            visited.append(str(item["id"]))
        seen_pages += 1
        if cursor is None:
            break
        if seen_pages > 10:  # guard against infinite loop in case of bugs
            pytest.fail("Pagination did not terminate.")
    assert len(visited) == 7
    # Every id should have been visited exactly once.
    assert len(set(visited)) == 7


def test_paginate_returns_none_cursor_when_exhausted() -> None:
    items = _make_items(2)
    _, cursor = paginate(
        items,
        cursor=None,
        limit=10,
        key=lambda x: (x["submitted_at"], x["id"]),
        order="desc",
    )
    assert cursor is None


def test_paginate_ascending_order() -> None:
    items = _make_items(5)
    page, _ = paginate(
        items,
        cursor=None,
        limit=2,
        key=lambda x: (x["submitted_at"], x["id"]),
        order="asc",
    )
    assert page[0]["submitted_at"] <= page[1]["submitted_at"]
