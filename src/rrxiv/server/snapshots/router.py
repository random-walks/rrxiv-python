"""Snapshots router — GET /snapshots/latest, POST /snapshots,
GET /snapshots/{id} for the blob."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
import uuid
from base64 import b64encode
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from rrxiv.server.deps import AuthedRequest, get_store, require_identity
from rrxiv.server.errors import forbidden, not_found
from rrxiv.server.store import AnonymousIdentity, Store

router = APIRouter(prefix="/snapshots", tags=["Snapshots"])

_REQUIRES_NAMED = require_identity(allow_anonymous=False)


@router.get("/latest")
def latest_snapshot(request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    manifest = store.latest_snapshot()
    if manifest is None:
        raise not_found("no snapshot has been published")
    return manifest


@router.post("", status_code=201)
def create_snapshot(
    request: Request,
    auth: AuthedRequest = Depends(_REQUIRES_NAMED),
) -> dict[str, Any]:
    """Build a snapshot tar.gz of the current corpus.

    Includes papers/<id>.json, cirs/<id>.json, claims/<id>.json,
    annotations/<id>.json, plus a manifest.json with a SHA-256
    Content-Digest over the rest.
    """
    if isinstance(auth.identity, AnonymousIdentity):
        raise forbidden("anonymous identities cannot create snapshots")

    store: Store = get_store(request)

    snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    # Build the tarball in memory.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for paper in store.list_papers():
            _add_json(tar, f"papers/{paper['id']}.json", paper)
        for claim in store.list_claims():
            _add_json(tar, f"claims/{claim['id']}.json", claim)
        for ann in store.list_annotations():
            _add_json(tar, f"annotations/{ann['id']}.json", ann)
        # CIRs separately so consumers can reconstruct without re-walking.
        for paper in store.list_papers():
            cir = store.get_cir(paper["id"])
            if cir is not None:
                _add_json(tar, f"cirs/{paper['id']}.json", cir)

    payload = buf.getvalue()
    digest = hashlib.sha256(payload).digest()
    content_digest = f"sha-256=:{b64encode(digest).decode('ascii')}:"

    manifest: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "created_at_unix": now,
        "content_digest": content_digest,
        "size_bytes": len(payload),
        "counts": {
            "papers": len(store.list_papers()),
            "claims": len(store.list_claims()),
            "annotations": len(store.list_annotations()),
        },
        "blob_uri": f"/api/v0/snapshots/{snapshot_id}/blob",
    }

    store.save_snapshot_blob(snapshot_id, payload)
    store.set_latest_snapshot(manifest)
    return manifest


@router.get("/{snapshot_id}/blob")
def snapshot_blob(snapshot_id: str, request: Request) -> Response:
    store: Store = get_store(request)
    blob = store.load_snapshot_blob(snapshot_id)
    if blob is None:
        raise not_found(f"snapshot {snapshot_id} not found")
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{snapshot_id}.tar.gz"'
        },
    )


def _add_json(tar: tarfile.TarFile, path: str, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    info = tarfile.TarInfo(name=path)
    info.size = len(raw)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(raw))
