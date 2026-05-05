"""Tests for the rrxiv.testing.mock_server."""

from __future__ import annotations

from typing import Any

import pytest

from rrxiv.client import (
    BearerToken,
    NotFoundError,
    RateLimitedError,
    RrxivClient,
    UnauthorizedError,
)
from rrxiv.testing import MockRrxivServer


def _paper(paper_id: str = "p1") -> dict[str, Any]:
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


def _claim(claim_id: str = "p1:c1", **overrides: Any) -> dict[str, Any]:
    base = {
        "id": claim_id,
        "statement": "X.",
        "claim_type": "theoretical",
        "evidence_type": "argument",
    }
    base.update(overrides)
    return base


def test_get_paper_round_trip() -> None:
    server = MockRrxivServer()
    server.add_paper(_paper("p1"))
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        paper = c.get_paper("p1")
    assert paper.id == "p1"


def test_get_cir_default_synthesised() -> None:
    server = MockRrxivServer()
    server.add_paper(_paper("p1"))
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        cir = c.get_cir("p1")
    assert cir.id == "p1"
    assert cir.annotations == []  # default


def test_404_for_unknown_paper() -> None:
    server = MockRrxivServer()
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        with pytest.raises(NotFoundError):
            c.get_paper("nope")


def test_list_paginated_papers() -> None:
    server = MockRrxivServer()
    server.add_paper(_paper("p1"))
    server.add_paper(_paper("p2"))
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        page = c.list_papers()
    ids = [p["id"] for p in page["items"]]
    assert sorted(ids) == ["p1", "p2"]


def test_claim_walk() -> None:
    server = MockRrxivServer()
    server.add_claim(_claim("p1:c1"))
    server.add_claim(_claim("p1:c2", depends_on=["p1:c1"]))
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        # outgoing depends-on from c2 should include c1
        walk = c.claim_depends_on("p1:c2")
        assert len(walk["edges"]) == 1
        assert walk["edges"][0]["target"] == "p1:c1"
        # reverse: dependents of c1 should include c2
        walk = c.claim_dependents("p1:c1")
        assert len(walk["edges"]) == 1
        assert walk["edges"][0]["source"] == "p1:c2"


def test_create_annotation_requires_auth() -> None:
    server = MockRrxivServer()
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        with pytest.raises(UnauthorizedError):
            c.create_annotation(
                {
                    "id": "ann-1",
                    "target_id": "p1:c1",
                    "target_type": "claim",
                    "annotation_type": "comment",
                    "content": ".",
                    "created_at": "2026-06-01T00:00:00Z",
                    "created_by": {"identity_type": "anonymous", "identity": "x"},
                }
            )


def test_create_annotation_with_auth_succeeds() -> None:
    server = MockRrxivServer()
    auth = BearerToken("tok", "orcid", "0000-0001-2345-6789")
    transport = server.transport
    with RrxivClient(
        "https://example.test/api/v0", transport=transport, auth=auth
    ) as c:
        ann = c.create_annotation(
            {
                "id": "ann-x",
                "target_id": "p1",
                "target_type": "paper",
                "annotation_type": "comment",
                "content": ".",
                "created_at": "2026-06-01T00:00:00Z",
                "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
            }
        )
    assert ann.id == "ann-x"
    assert "ann-x" in server.annotations


def test_rate_limit_after() -> None:
    server = MockRrxivServer(rate_limit_after=0)
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        with pytest.raises(RateLimitedError):
            c.get_paper("p1")


def test_request_count_increments() -> None:
    server = MockRrxivServer()
    server.add_paper(_paper("p1"))
    with RrxivClient("https://example.test/api/v0", transport=server.transport) as c:
        c.get_paper("p1")
        c.get_paper("p1")
    assert server.request_count == 2
