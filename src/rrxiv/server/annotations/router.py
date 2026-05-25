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

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ValidationError

from rrxiv.server.annotations.threads import list_direct_replies, validate_in_reply_to
from rrxiv.server.deps import AuthedRequest, get_store, require_identity
from rrxiv.server.errors import (
    bad_request,
    conflict,
    forbidden,
    not_found,
    validation_error,
)
from rrxiv.server.observability import breadcrumb, tag
from rrxiv.server.pagination import paginate
from rrxiv.server.store import (
    AnonymousIdentity,
    IdempotencyEntry,
    Store,
)

# Claim IDs follow the canonical shape ``<paper_id>:<local_id>`` per
# spec/0003-claim-graph.md. ``local_id`` is author-chosen via
# ``\label{...}`` and may itself contain colons (e.g. ``prop:I.10``),
# dots (``thm:main``), or dashes (``claim-4``). Earlier the regex
# required ``:c<n>`` — that's the parser's default when no label is
# present, not a protocol requirement. Loosened to accept any
# reasonably-shaped label.
_CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.:\-]+$")

router = APIRouter(prefix="/annotations", tags=["Annotations"])

# FastAPI dependency callables. Hoisted to module level so the per-route
# `Depends(...)` arg-default doesn't trip ruff's B008 (which warns about
# function calls in defaults — though Depends is the idiomatic FastAPI
# form). The real fix is to bake one auth dep per identity profile here.
_REQUIRES_NAMED_IDENTITY = require_identity(allow_anonymous=False)


@router.get("")
def list_annotations(
    request: Request,
    target_id: str | None = Query(
        default=None,
        description="Filter to annotations on this paper or claim.",
    ),
    target_type: str | None = Query(
        default=None,
        description="One of 'paper' or 'claim'.",
    ),
    annotation_type: str | None = Query(
        default=None,
        description="Filter by annotation_type (replication/contradiction/erratum/comment/…).",
    ),
    created_by_identity_type: str | None = Query(
        default=None,
        alias="created_by_identity_type",
        description="Filter by the identity_type of created_by (orcid/agent/anonymous).",
    ),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    store: Store = get_store(request)
    items = list(store.list_annotations())
    if target_id is not None:
        items = [a for a in items if a.get("target_id") == target_id]
    if target_type is not None:
        items = [a for a in items if a.get("target_type") == target_type]
    if annotation_type is not None:
        items = [a for a in items if a.get("annotation_type") == annotation_type]
    if created_by_identity_type is not None:
        items = [
            a for a in items
            if isinstance(a.get("created_by"), dict)
            and a["created_by"].get("identity_type") == created_by_identity_type
        ]

    page, next_cursor = paginate(
        items,
        cursor=cursor,
        limit=limit,
        key=lambda a: (a.get("created_at") or "", a.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/{ann_id}")
def get_annotation(ann_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    a = store.get_annotation(ann_id)
    if a is None:
        raise not_found(f"annotation {ann_id} not found")
    return a


@router.get("/{ann_id}/replies")
def get_annotation_replies(
    ann_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    """Direct replies (``in_reply_to == ann_id``), oldest first (RRP-0018)."""
    store: Store = get_store(request)
    if store.get_annotation(ann_id) is None:
        raise not_found(f"annotation {ann_id} not found")
    items = list_direct_replies(store, ann_id)
    page, next_cursor = paginate(
        items,
        cursor=cursor,
        limit=limit,
        key=lambda a: (a.get("created_at") or "", a.get("id") or ""),
        order="asc",
    )
    return {"items": page, "next_cursor": next_cursor}


class _IncomingAnnotation(BaseModel):
    """Loose schema — the rrxiv Annotation pydantic model is exhaustive
    but we want the server to be permissive about evolving fields."""

    model_config = {"extra": "allow"}

    id: str | None = None


_BULK_MAX_PER_REQUEST = 100


@router.post("/bulk", status_code=200)
async def create_annotations_bulk(
    body: dict[str, Any],
    request: Request,
    auth: AuthedRequest = Depends(_REQUIRES_NAMED_IDENTITY),
) -> dict[str, Any]:
    """Post up to 100 annotations in a single request (Sprint 19 P3).

    Body shape: ``{"annotations": [Annotation, Annotation, ...]}``.

    Returns ``{"results": [{"index": N, "status": 201, "body": {...}}
    | {"index": N, "status": 422, "error": {...}}]}``. Best-effort
    semantics: each annotation is validated + persisted independently,
    so a partial success leaves the valid annotations in the store and
    surfaces the failures per-index. The HTTP status of the bulk call
    itself is always 200; readers check ``results[i].status``.

    Counts as a **single** request against the per-identity rate limit
    regardless of payload size. This is the whole point — Sprint 16's
    44 sequential POSTs tripped the 30-rpm limiter; a single bulk call
    completes the same work in one budget unit.

    Same auth posture as ``POST /annotations`` (named identity,
    anonymous forbidden). No idempotency key on the bulk call itself
    — each inner annotation may carry its own ``id`` for client-side
    dedup on retries.
    """
    if isinstance(auth.identity, AnonymousIdentity):
        raise forbidden("anonymous identities cannot create annotations")

    raw = body.get("annotations")
    if not isinstance(raw, list):
        raise bad_request("body must have an 'annotations' array")
    if len(raw) == 0:
        return {"results": []}
    if len(raw) > _BULK_MAX_PER_REQUEST:
        raise bad_request(
            f"bulk accepts at most {_BULK_MAX_PER_REQUEST} annotations per request "
            f"(got {len(raw)}); split into multiple calls"
        )

    store: Store = get_store(request)

    from rrxiv.models import Annotation

    results: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            results.append(
                {
                    "index": idx,
                    "status": 422,
                    "error": "annotation must be an object",
                }
            )
            continue

        if "id" not in item or not item.get("id"):
            item["id"] = f"ann-{uuid.uuid4().hex[:10]}"
        if "created_by" not in item:
            results.append(
                {
                    "index": idx,
                    "status": 400,
                    "error": "annotation.created_by is required",
                }
            )
            continue

        try:
            Annotation.model_validate(item)
        except ValidationError as e:
            results.append(
                {
                    "index": idx,
                    "status": 422,
                    "error": json.loads(e.json()),
                }
            )
            continue

        # Mirror the singleton endpoint's cross-reference checks.
        target_type = item.get("target_type")
        target_id = item.get("target_id")
        if target_type == "claim":
            if not isinstance(target_id, str) or not _CLAIM_ID_PATTERN.match(
                target_id
            ):
                results.append(
                    {
                        "index": idx,
                        "status": 422,
                        "error": (
                            f"target_id {target_id!r} for target_type=claim "
                            "must match <paper_id>:c<n>"
                        ),
                    }
                )
                continue
            if store.get_claim(target_id) is None:
                results.append(
                    {
                        "index": idx,
                        "status": 404,
                        "error": f"claim {target_id} not found",
                    }
                )
                continue
        elif target_type == "paper" and (
            not isinstance(target_id, str) or store.get_paper(target_id) is None
        ):
            results.append(
                {
                    "index": idx,
                    "status": 404,
                    "error": f"paper {target_id} not found",
                }
            )
            continue

        try:
            validate_in_reply_to(store, item)
        except Exception as e:
            results.append(
                {
                    "index": idx,
                    "status": 400,
                    "error": str(e),
                }
            )
            continue

        store.add_annotation(item)
        results.append({"index": idx, "status": 201, "body": item})

        # Sprint 21: bulk path counts each successfully-persisted
        # annotation toward the same counter the singleton POST uses,
        # so the dashboard's annotation volume reflects total work
        # regardless of whether agents submit one-at-a-time or batched.
        try:
            from rrxiv.server.metrics import annotations_posted_total

            annotations_posted_total.labels(
                annotation_type=item.get("annotation_type") or "unknown",
                auth_kind=auth.tier,
                target_kind=item.get("target_type") or "unknown",
            ).inc()
        except Exception:
            pass

    return {"results": results}


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

    # Stamp Sentry with the rrxiv-domain context — invaluable when a
    # 500 fires inside the validation cascade below.
    tag("annotation_type", body.get("annotation_type"))
    tag("target_kind", body.get("target_type"))
    breadcrumb(
        "annotation",
        "POST /annotations entered",
        data={
            "annotation_type": body.get("annotation_type"),
            "target_kind": body.get("target_type"),
            "has_idempotency_key": idempotency_key is not None,
        },
    )

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
        breadcrumb(
            "annotation",
            "schema validation failed",
            level="warning",
            data={"first_error": (e.errors() or [{}])[0].get("msg", "")},
        )
        raise validation_error(
            "annotation failed schema validation",
            extra={"errors": json.loads(e.json())},
        ) from e
    breadcrumb("annotation", "schema validated")

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

    # Threading (RRP-0018): in_reply_to must exist, share an artefact,
    # and not be a self-reply.
    validate_in_reply_to(store, body)

    store.add_annotation(body)
    breadcrumb("annotation", "persisted")

    # Sprint 21 metric: post-persist so failed annotations don't count.
    try:
        from rrxiv.server.metrics import annotations_posted_total

        annotations_posted_total.labels(
            annotation_type=body.get("annotation_type") or "unknown",
            auth_kind=auth.tier,
            target_kind=body.get("target_type") or "unknown",
        ).inc()
    except Exception:
        pass

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
