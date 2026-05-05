"""In-process mock rrxiv server.

Implements just enough of the API surface (per
``rrxiv/schema/api.openapi.yaml``) to exercise the client end-to-end:

- GET /version
- GET /papers, GET /papers/{id}, GET /papers/{id}/cir
- GET /claims, GET /claims/{id}, GET /claims/{id}/depends-on,
  GET /claims/{id}/dependents
- GET /annotations, GET /annotations/{id}
- POST /annotations (with auth + Idempotency-Key)
- GET /snapshots/latest

Backed by in-memory dicts that tests can pre-populate via
:py:meth:`add_paper`, :py:meth:`add_claim`, :py:meth:`add_annotation`,
:py:meth:`set_latest_snapshot`. The mock is intentionally permissive
about what it accepts as request payloads; it's not a conformance
implementation, just a stand-in that lets client tests do round trips.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from rrxiv.client.client import _gen_idempotency_key  # re-use


@dataclass
class MockRrxivServer:
    """In-memory rrxiv API mock for tests.

    Attributes:
        papers: paper_id → paper record (a dict matching paper.schema.json).
        cirs: paper_id → CIR record (cir.schema.json).
        claims: claim_id → claim record (claim.schema.json).
        annotations: annotation_id → annotation record.
        require_auth_for_writes: if True, POST endpoints return 401
            unless the request has an Authorization header. Default
            True (matches the protocol).
        rate_limit_after: if not None, after this many requests the
            mock starts returning 429 with Retry-After: 0. Useful for
            exercising client retry policies. Default None (off).
    """

    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    cirs: dict[str, dict[str, Any]] = field(default_factory=dict)
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_snapshot: dict[str, Any] | None = None
    require_auth_for_writes: bool = True
    rate_limit_after: int | None = None

    request_count: int = 0
    """How many requests the mock has served. Tests can introspect."""

    # ------------------------------------------------------------------
    # Population helpers
    # ------------------------------------------------------------------

    def add_paper(self, paper: dict[str, Any]) -> None:
        """Register a paper. Stores both the metadata view (papers/) and
        a default CIR view (papers/cir) — the CIR is the same payload
        plus an empty annotations array if one isn't provided."""
        self.papers[paper["id"]] = dict(paper)
        if paper["id"] not in self.cirs:
            cir = dict(paper)
            cir.setdefault("annotations", [])
            self.cirs[paper["id"]] = cir

    def add_cir(self, cir: dict[str, Any]) -> None:
        """Register a full CIR (used when the metadata view and the CIR
        diverge — e.g., the CIR has claims/citations/annotations the
        metadata-only view doesn't)."""
        self.cirs[cir["id"]] = dict(cir)
        # Derive a metadata view too if absent
        if cir["id"] not in self.papers:
            metadata = {
                k: v
                for k, v in cir.items()
                if k not in ("claims", "citations", "annotations", "sections", "figures")
            }
            self.papers[cir["id"]] = metadata

    def add_claim(self, claim: dict[str, Any]) -> None:
        self.claims[claim["id"]] = dict(claim)

    def add_annotation(self, annotation: dict[str, Any]) -> None:
        self.annotations[annotation["id"]] = dict(annotation)

    def set_latest_snapshot(self, manifest: dict[str, Any]) -> None:
        self.latest_snapshot = dict(manifest)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        """An ``httpx.MockTransport`` to plug into ``RrxivClient(transport=...)``."""
        return httpx.MockTransport(self._handle)

    @property
    def async_transport(self) -> httpx.MockTransport:
        """Async variant. ``httpx.MockTransport`` works for both sync and async."""
        return httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        if (
            self.rate_limit_after is not None
            and self.request_count > self.rate_limit_after
        ):
            return httpx.Response(429, headers={"Retry-After": "0"})

        path = request.url.path
        # Strip a /api/v0 prefix if present so tests can configure either
        # base URL form.
        for prefix in ("/api/v0", "/api/v1"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break

        method = request.method.upper()

        if method == "GET" and path == "/version":
            return _json_ok(
                {
                    "server": "MockRrxivServer/0.1",
                    "protocol": "0.1.0",
                    "supported_api_versions": ["v0"],
                }
            )

        # Papers
        if method == "GET" and path == "/papers":
            return _paginated(list(self.papers.values()))
        m = re.fullmatch(r"/papers/([^/]+)", path)
        if method == "GET" and m:
            paper_id = m.group(1)
            paper = self.papers.get(paper_id)
            if paper is None:
                return _not_found(f"paper {paper_id}")
            return _json_ok(paper)
        m = re.fullmatch(r"/papers/([^/]+)/cir", path)
        if method == "GET" and m:
            paper_id = m.group(1)
            cir = self.cirs.get(paper_id)
            if cir is None:
                return _not_found(f"paper {paper_id}")
            return _json_ok(cir)

        # Claims
        if method == "GET" and path == "/claims":
            return _paginated(list(self.claims.values()))
        m = re.fullmatch(r"/claims/([^/]+)", path)
        if method == "GET" and m:
            cid = m.group(1)
            claim = self.claims.get(cid)
            if claim is None:
                return _not_found(f"claim {cid}")
            return _json_ok(claim)
        m = re.fullmatch(r"/claims/([^/]+)/(depends-on|dependents)", path)
        if method == "GET" and m:
            cid, walk_kind = m.group(1), m.group(2)
            return _json_ok({"origin": cid, "edges": self._walk_edges(cid, walk_kind)})

        # Annotations
        if method == "GET" and path == "/annotations":
            return _paginated(list(self.annotations.values()))
        m = re.fullmatch(r"/annotations/([^/]+)", path)
        if method == "GET" and m:
            ann_id = m.group(1)
            ann = self.annotations.get(ann_id)
            if ann is None:
                return _not_found(f"annotation {ann_id}")
            return _json_ok(ann)
        if method == "POST" and path == "/annotations":
            if self.require_auth_for_writes and not request.headers.get("Authorization"):
                return _problem(401, "Unauthorized", "missing bearer token")
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                return _problem(400, "Bad Request", "body is not JSON")
            ann_id = body.get("id") or f"ann-{uuid.uuid4().hex[:8]}"
            body["id"] = ann_id
            self.annotations[ann_id] = body
            return _json_ok(body, status=201)

        # Snapshots
        if method == "GET" and path == "/snapshots/latest":
            if self.latest_snapshot is None:
                return _not_found("latest snapshot")
            return _json_ok(self.latest_snapshot)

        # Default: 404 with a helpful message
        return _problem(404, "Not Found", f"{method} {path} not handled by mock")

    def _walk_edges(self, claim_id: str, walk_kind: str) -> list[dict[str, str]]:
        """Synthesise a one-hop edge walk from the in-memory claims."""
        edges: list[dict[str, str]] = []
        if walk_kind == "depends-on":
            origin_claim = self.claims.get(claim_id)
            for target in (origin_claim or {}).get("depends_on", []) or []:
                edges.append({"source": claim_id, "target": target, "kind": "depends_on"})
        elif walk_kind == "dependents":
            for cid, claim in self.claims.items():
                if claim_id in (claim.get("depends_on") or []):
                    edges.append({"source": cid, "target": claim_id, "kind": "depends_on"})
        return edges


def _json_ok(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _paginated(items: list[Any]) -> httpx.Response:
    return _json_ok({"items": items, "next_cursor": None})


def _not_found(what: str) -> httpx.Response:
    return _problem(404, "Not Found", f"{what} not found")


def _problem(status: int, title: str, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(
            {"type": "https://rrxiv.com/errors/mock", "title": title, "detail": detail}
        ).encode("utf-8"),
        headers={"content-type": "application/problem+json"},
    )


# Re-export to keep type-hint imports happy
__all__ = ["MockRrxivServer", "_gen_idempotency_key"]
