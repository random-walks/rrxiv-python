"""Tests for the AsyncRrxivClient."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from rrxiv.client import (
    AsyncRrxivClient,
    BearerToken,
    NotFoundError,
    RateLimitedError,
    RetryPolicy,
    UnauthorizedError,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _no_real_async_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_s: float) -> None:
        return None

    monkeypatch.setattr("rrxiv.client.async_client._async_sleep_for", _noop)


def _client(handler: Handler, retry_policy: RetryPolicy | None = None) -> AsyncRrxivClient:
    return AsyncRrxivClient(
        "https://rrxiv.com/api/v0",
        transport=httpx.MockTransport(handler),
        retry_policy=retry_policy,
    )


def _paper_payload(paper_id: str = "p1") -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "T",
        "authors": [{"name": "A. Author"}],
        "abstract": "x",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
    }


def _annotation_payload(ann_id: str = "ann-1") -> dict[str, Any]:
    return {
        "id": ann_id,
        "target_id": "p1:c1",
        "target_type": "claim",
        "annotation_type": "comment",
        "content": "x",
        "created_at": "2026-06-01T08:30:00Z",
        "created_by": {"identity_type": "anonymous", "identity": "x"},
    }


# ---- Happy paths ----


@pytest.mark.asyncio
async def test_get_paper() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_paper_payload())

    async with _client(handler) as client:
        paper = await client.get_paper("p1")
    assert paper.id == "p1"


@pytest.mark.asyncio
async def test_list_papers_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [_paper_payload()], "next_cursor": None}
        )

    async with _client(handler) as client:
        page = await client.list_papers(limit=10)
    assert page["items"][0]["id"] == "p1"


@pytest.mark.asyncio
async def test_create_annotation_with_auth_and_idempotency() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["idem"] = request.headers.get("Idempotency-Key", "")
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(201, json=_annotation_payload())

    auth = BearerToken("tok", "orcid", "0000-0001-2345-6789")
    transport = httpx.MockTransport(handler)
    async with AsyncRrxivClient(
        "https://rrxiv.com/api/v0", auth=auth, transport=transport
    ) as client:
        ann = await client.create_annotation(_annotation_payload(), idempotency_key="key-1")
    assert ann.id == "ann-1"
    assert captured["idem"] == "key-1"
    assert captured["auth"] == "Bearer tok"


# ---- Pagination iterator ----


@pytest.mark.asyncio
async def test_iter_papers_follows_cursors() -> None:
    pages = iter(
        [
            {"items": [_paper_payload("p1"), _paper_payload("p2")], "next_cursor": "c2"},
            {"items": [_paper_payload("p3")], "next_cursor": None},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(pages))

    seen: list[str] = []
    async with _client(handler) as client:
        async for paper in client.iter_papers():
            seen.append(paper["id"])
    assert seen == ["p1", "p2", "p3"]


# ---- Errors ----


@pytest.mark.asyncio
async def test_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            content=json.dumps({"title": "Not found", "detail": "."}).encode(),
            headers={"content-type": "application/problem+json"},
        )

    async with _client(handler) as client:
        with pytest.raises(NotFoundError):
            await client.get_paper("nope")


@pytest.mark.asyncio
async def test_401_raises_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _client(handler) as client:
        with pytest.raises(UnauthorizedError):
            await client.get_paper("p1")


# ---- Retry ----


@pytest.mark.asyncio
async def test_async_retry_on_429() -> None:
    calls: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_paper_payload())

    async with _client(handler) as client:
        paper = await client.get_paper("p1")
    assert paper.id == "p1"
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_async_gives_up_after_max_retries() -> None:
    calls: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    policy = RetryPolicy(
        max_retries=2, backoff_initial_seconds=0, backoff_max_seconds=0, jitter=0.0
    )
    async with _client(handler, retry_policy=policy) as client:
        with pytest.raises(RateLimitedError):
            await client.get_paper("p1")
    assert calls[0] == 3
