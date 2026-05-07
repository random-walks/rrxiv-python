"""Submissions router — POST /submissions, plus paper source +
versions endpoints (RRP-0008 / OpenAPI alignment)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from rrxiv.server.deps import AuthedRequest, get_store, require_identity
from rrxiv.server.errors import (
    bad_request,
    forbidden,
    not_found,
    validation_error,
)
from rrxiv.server.store import (
    AnonymousIdentity,
    Store,
)

router = APIRouter(tags=["Papers"])

# Module-level dep singleton — RRP-0008 §"Auth identity resolution"
# uses one builder per identity profile.
_REQUIRES_NAMED_IDENTITY = require_identity(allow_anonymous=False)


@router.post("/submissions", status_code=201)
async def submit_paper(
    request: Request,
    cir: UploadFile = File(...),
    bundle: UploadFile = File(...),
    previous_version: str | None = Form(default=None),
    auth: AuthedRequest = Depends(_REQUIRES_NAMED_IDENTITY),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Submit a paper.

    The request is multipart/form-data with two file fields:

    - ``cir``: the client-computed Canonical Intermediate Representation
      JSON. The server validates it against ``cir.schema.json``.
    - ``bundle``: the source archive (tar.gz). Persisted to the store
      and reachable via ``GET /papers/{id}/source``.

    On success the server returns ``{paper_id, retrieval_uri}``.
    """
    if isinstance(auth.identity, AnonymousIdentity):
        raise forbidden("anonymous identities cannot submit papers")

    # Read both files fully into memory. v0.1 reference server scope.
    cir_bytes = await cir.read()
    bundle_bytes = await bundle.read()

    # CIR must parse as JSON.
    try:
        cir_data = json.loads(cir_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise bad_request(f"cir is not valid UTF-8 JSON: {e}") from e

    # Validate against the rrxiv.models.CIR pydantic model — that's the
    # canonical schema-derived shape. Errors surface as 422 detail.
    from pydantic import ValidationError

    from rrxiv.models import CIR

    try:
        cir_obj = CIR.model_validate(cir_data)
    except ValidationError as e:
        raise validation_error(
            "cir failed schema validation",
            extra={"errors": json.loads(e.json())},
        ) from e

    store: Store = get_store(request)

    # ID assignment: if the CIR carries an `id` use it (revisions); else
    # mint a fresh one. Per spec/0005-submission.md, server is the
    # authority on paper IDs for new submissions; we honour client IDs
    # only for the revision-of path.
    paper_id = cir_obj.id or _mint_paper_id()
    cir_data["id"] = paper_id
    if previous_version:
        cir_data["previous_version"] = previous_version

    # Idempotency.
    if idempotency_key:
        existing = store.get_idempotency(auth.token, idempotency_key)
        if existing is not None:
            return existing.response_body

    paper_metadata = {
        k: v
        for k, v in cir_data.items()
        if k not in ("claims", "citations", "annotations", "sections", "figures")
    }
    store.add_paper(paper_metadata)
    store.add_cir(cir_data)
    source_uri = store.save_source(paper_id, bundle_bytes)

    response_body = {
        "paper_id": paper_id,
        "retrieval_uri": source_uri,
    }

    if idempotency_key:
        from rrxiv.server.store import IdempotencyEntry

        store.add_idempotency(
            auth.token,
            idempotency_key,
            IdempotencyEntry(
                body_sha256="",  # multipart bodies are not hashed for replay
                response_status=201,
                response_body=response_body,
                created_at_unix=int(time.time()),
            ),
        )

    return response_body


def _mint_paper_id() -> str:
    return f"paper-{uuid.uuid4().hex[:12]}"


# ---- Source download + versions live on /papers/{id}/* ----


sources_router = APIRouter(prefix="/papers", tags=["Papers"])


@sources_router.get("/{paper_id}/source")
def get_paper_source(paper_id: str, request: Request) -> Response:
    """Stream a paper's source archive."""
    store: Store = get_store(request)
    if store.get_paper(paper_id) is None:
        raise not_found(f"paper {paper_id} not found")
    blob = store.load_source(paper_id)
    if blob is None:
        raise not_found(f"paper {paper_id} has no source archive")
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{paper_id}.tar.gz"'
        },
    )


@sources_router.get("/{paper_id}/versions")
def get_paper_versions(paper_id: str, request: Request) -> dict[str, Any]:
    """Return the chain of versions for ``paper_id`` ordered oldest-first.

    Walks ``previous_version`` pointers in the in-memory paper records.
    """
    store: Store = get_store(request)
    paper = store.get_paper(paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")

    # Walk forward and backward to find the head + tail of the chain.
    # We expose the chain rooted at the queried paper for simplicity.
    chain: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = paper
    while cur is not None:
        chain.append(
            {
                "id": cur["id"],
                "version": cur.get("version"),
                "submitted_at": cur.get("submitted_at"),
                "previous_version": cur.get("previous_version"),
            }
        )
        prev_id = cur.get("previous_version")
        if not prev_id:
            break
        cur = store.get_paper(prev_id)
    chain.reverse()
    return {"items": chain}
