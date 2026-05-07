"""Annotations router — read public, create requires auth.

Strict validation per Phase 3:

- Body validates against ``rrxiv.models.Annotation`` (which embeds
  the per-type ``structured_payload`` validators from
  ``rrxiv.annotations``). Errors surface as RFC 9457 422 with the
  pydantic detail attached.
- For ``target_type=claim``, ``target_id`` must follow the
  ``<paper_id>:c<n>`` shape and the referenced claim must exist in
  the store. 422 on shape mismatch; 404 on missing claim.
- For ``target_type=paper``, the paper must exist; 404 otherwise.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ValidationError

from rrxiv.server.deps import AuthedRequest, get_store, require_identity
from rrxiv.server.errors import (
    bad_request,
    conflict,
    forbidden,
    not_found,
    validation_error,
)
from rrxiv.server.store import (
    AnonymousIdentity,
    IdempotencyEntry,
    Store,
)

_CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+:c\d+$")

router = APIRouter(prefix="/annotations", tags=["Annotations"])

# FastAPI dependency callables. Hoisted to module level so the per-route
# `Depends(...)` arg-default doesn't trip ruff's B008 (which warns about
# function calls in defaults — though Depends is the idiomatic FastAPI
# form). The real fix is to bake one auth dep per identity profile here.
_REQUIRES_NAMED_IDENTITY = require_identity(allow_anonymous=False)


@router.get("")
def list_annotations(request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    return {"items": store.list_annotations(), "next_cursor": None}


@router.get("/{ann_id}")
def get_annotation(ann_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    a = store.get_annotation(ann_id)
    if a is None:
        raise not_found(f"annotation {ann_id} not found")
    return a


class _IncomingAnnotation(BaseModel):
    """Loose schema — the rrxiv Annotation pydantic model is exhaustive
    but we want the server to be permissive about evolving fields."""

    model_config = {"extra": "allow"}

    id: str | None = None


@router.post("", status_code=201)
async def create_annotation(
    body: dict[str, Any],
    request: Request,
    auth: AuthedRequest = Depends(_REQUIRES_NAMED_IDENTITY),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    store: Store = get_store(request)

    if isinstance(auth.identity, AnonymousIdentity):
        raise forbidden("anonymous identities cannot create annotations")

    # Idempotency: if (token, key) is known with the same body, return
    # the cached response. If known with a *different* body, 409.
    raw_body = await request.body()
    body_hash = hashlib.sha256(raw_body).hexdigest()
    if idempotency_key:
        existing = store.get_idempotency(auth.token, idempotency_key)
        if existing is not None:
            if existing.body_sha256 != body_hash:
                raise conflict(
                    f"idempotency key {idempotency_key!r} previously used "
                    f"with a different request body"
                )
            return existing.response_body

    if "id" not in body or not body.get("id"):
        body["id"] = f"ann-{uuid.uuid4().hex[:10]}"
    if "created_by" not in body:
        raise bad_request("annotation.created_by is required")

    # Strict validation against the canonical pydantic model. The model
    # itself runs the per-type structured_payload validators imported
    # from rrxiv.annotations.
    from rrxiv.models import Annotation

    try:
        Annotation.model_validate(body)
    except ValidationError as e:
        raise validation_error(
            "annotation failed schema validation",
            extra={"errors": json.loads(e.json())},
        ) from e

    # Cross-reference target existence + shape (Phase 3).
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    if target_type == "claim":
        if not isinstance(target_id, str) or not _CLAIM_ID_PATTERN.match(target_id):
            raise validation_error(
                f"target_id {target_id!r} for target_type=claim must "
                "match <paper_id>:c<n>"
            )
        if store.get_claim(target_id) is None:
            raise not_found(f"claim {target_id} not found")
    elif target_type == "paper":
        if not isinstance(target_id, str) or store.get_paper(target_id) is None:
            raise not_found(f"paper {target_id} not found")

    store.add_annotation(body)

    if idempotency_key:
        store.add_idempotency(
            auth.token,
            idempotency_key,
            IdempotencyEntry(
                body_sha256=body_hash,
                response_status=201,
                response_body=body,
                created_at_unix=int(time.time()),
            ),
        )

    return body
