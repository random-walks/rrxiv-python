"""Tests for the rrxiv HTTP client.

Use httpx's MockTransport so tests are deterministic and don't need a
running server. The client surfaces typed errors per status code; we
exercise each path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from rrxiv.client import (
    BearerToken,
    NotFoundError,
    RateLimitedError,
    RrxivClient,
    ServerError,
    UnauthorizedError,
    ValidationError,
)
from rrxiv.client.client import _gen_idempotency_key

# ---- Fixtures ----


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


def _claim_payload(claim_id: str = "p1:c1") -> dict[str, Any]:
    return {
        "id": claim_id,
        "statement": "X.",
        "claim_type": "theoretical",
        "evidence_type": "argument",
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


Handler = Callable[[httpx.Request], httpx.Response]


def _client_with_handler(handler: Handler) -> RrxivClient:
    transport = httpx.MockTransport(handler)
    return RrxivClient("https://rrxiv.com/api/v0", transport=transport)


# ---- Happy paths ----


class TestHappyPath:
    def test_get_paper(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v0/papers/p1"
            return httpx.Response(200, json=_paper_payload())

        with _client_with_handler(handler) as client:
            paper = client.get_paper("p1")
        assert paper.id == "p1"

    def test_get_cir(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v0/papers/p1/cir"
            return httpx.Response(200, json={**_paper_payload(), "annotations": []})

        with _client_with_handler(handler) as client:
            cir = client.get_cir("p1")
        assert cir.id == "p1"

    def test_list_papers_pagination_params(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            assert params == {"limit": "5", "cursor": "abc"}
            return httpx.Response(
                200, json={"items": [_paper_payload()], "next_cursor": None}
            )

        with _client_with_handler(handler) as client:
            page = client.list_papers(limit=5, cursor="abc")
        assert page["items"][0]["id"] == "p1"

    def test_get_claim(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_claim_payload())

        with _client_with_handler(handler) as client:
            c = client.get_claim("p1:c1")
        assert c.id == "p1:c1"

    def test_claim_depends_on_includes_depth(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["depth"] == "3"
            return httpx.Response(200, json={"origin": "p1:c1", "edges": []})

        with _client_with_handler(handler) as client:
            walk = client.claim_depends_on("p1:c1", depth=3)
        assert walk["origin"] == "p1:c1"

    def test_create_annotation_attaches_idempotency_key(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["idem"] = request.headers.get("Idempotency-Key", "")
            captured["auth"] = request.headers.get("Authorization", "")
            body = json.loads(request.content)
            assert body["id"] == "ann-1"
            return httpx.Response(201, json=_annotation_payload())

        auth = BearerToken("tok-x", "orcid", "0000-0001-2345-6789")
        transport = httpx.MockTransport(handler)
        with RrxivClient(
            "https://rrxiv.com/api/v0", auth=auth, transport=transport
        ) as client:
            ann = client.create_annotation(_annotation_payload(), idempotency_key="my-key")
        assert ann.id == "ann-1"
        assert captured["idem"] == "my-key"
        assert captured["auth"] == "Bearer tok-x"

    def test_create_annotation_auto_idempotency_key(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["idem"] = request.headers.get("Idempotency-Key", "")
            return httpx.Response(201, json=_annotation_payload())

        with _client_with_handler(handler) as client:
            client.create_annotation(_annotation_payload())
        assert captured["idem"].startswith("rrxiv-py-")


# ---- Error paths ----


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "exc"),
        [
            (401, UnauthorizedError),
            (404, NotFoundError),
            (422, ValidationError),
            (500, ServerError),
        ],
    )
    def test_status_to_exc(
        self, status: int, exc: type[Exception]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                content=json.dumps(
                    {"type": "https://x", "title": "boom", "detail": "boom-detail"}
                ).encode(),
                headers={"content-type": "application/problem+json"},
            )

        with _client_with_handler(handler) as client:
            with pytest.raises(exc):
                client.get_paper("p1")

    def test_429_retry_after(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": "60"},
                content=b"",
            )

        with _client_with_handler(handler) as client:
            with pytest.raises(RateLimitedError) as exc_info:
                client.get_paper("p1")
        assert exc_info.value.retry_after_seconds == 60


# ---- Helpers ----


class TestHelpers:
    def test_idempotency_key_unique(self) -> None:
        assert _gen_idempotency_key() != _gen_idempotency_key()
        assert _gen_idempotency_key().startswith("rrxiv-py-")
