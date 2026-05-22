"""Submissions router — POST /submissions, plus paper source +
versions endpoints (RRP-0008 / OpenAPI alignment).

Multipart shape follows ``schema/submission_request.schema.json`` (RRP-0016).
On revision submission, the response carries an inline ``revision_diff``
(RRP-0017); on ``dry_run=true``, the server performs validation +
parse + diff without persisting.
"""

from __future__ import annotations

import hashlib
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
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response

from rrxiv.server.deps import AuthedRequest, get_store, require_identity
from rrxiv.server.errors import (
    bad_request,
    forbidden,
    not_found,
    validation_error,
)
from rrxiv.server.papers.diff import compute_revision_diff
from rrxiv.server.store import (
    AnonymousIdentity,
    Store,
)

router = APIRouter(tags=["Papers"])

# Module-level dep singleton — RRP-0008 §"Auth identity resolution"
# uses one builder per identity profile.
_REQUIRES_NAMED_IDENTITY = require_identity(allow_anonymous=False)


@router.post("/submissions")
async def submit_paper(
    request: Request,
    cir: UploadFile = File(...),
    bundle: UploadFile = File(...),
    previous_version: str | None = Form(default=None),
    revision_summary: str | None = Form(default=None),
    dry_run: str | None = Form(default=None),
    client_compile_hash: str | None = Form(default=None),
    auth: AuthedRequest = Depends(_REQUIRES_NAMED_IDENTITY),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """Submit a paper or revision (RRP-0016).

    Multipart fields:

    - ``cir``: client-computed CIR JSON.
    - ``bundle``: source archive (.tar.gz). Persisted to the store and
      reachable via ``GET /papers/{id}/source``.
    - ``previous_version``: paper_id of the prior version (revisions only).
    - ``revision_summary``: optional plaintext describing the changes;
      the server synthesises a ``revision_summary`` annotation (RRP-0017).
    - ``dry_run``: ``"true"`` to validate without persisting (RRP-0016
      §Dry-run semantics).
    - ``client_compile_hash``: optional SHA-256 of the bundle bytes.
      The server recomputes and rejects on mismatch.

    Response on persist (201) or dry-run (200): ``paper_id``,
    ``id_slug``, ``version``, ``previous_version``, ``retrieval_uri``,
    optional ``revision_diff``, ``would_persist``, ``dry_run``.
    """
    if isinstance(auth.identity, AnonymousIdentity):
        raise forbidden("anonymous identities cannot submit papers")

    is_dry_run = (dry_run or "").lower() in ("true", "1", "yes")

    # Read both files fully into memory. v0.1 reference server scope.
    cir_bytes = await cir.read()
    bundle_bytes = await bundle.read()

    # client_compile_hash check (RRP-0016).
    if client_compile_hash:
        actual = hashlib.sha256(bundle_bytes).hexdigest()
        if actual.lower() != client_compile_hash.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "bundle_hash_mismatch",
                    "message": (
                        "client_compile_hash does not match the SHA-256 of "
                        f"the uploaded bundle (got {actual}, expected "
                        f"{client_compile_hash})"
                    ),
                },
            )

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

    # Collision guard: each version must have a UNIQUE paper_id. If the
    # CIR's id matches the previous_version, the new revision would
    # overwrite its predecessor and introduce a self-loop in the
    # lineage chain (paper.id == paper.previous_version). Mint a fresh
    # paper_id instead — the slug stays stable across versions (per
    # RRP-0013), only the per-version internal id changes.
    if previous_version and paper_id == previous_version:
        paper_id = _mint_paper_id()

    cir_data["id"] = paper_id
    if previous_version:
        cir_data["previous_version"] = previous_version

    # id_slug: server-minted on first submission (RRP-0013). A revision
    # inherits its slug from the previous version — slugs are stable for
    # the paper's identity. If the CIR somehow already carries a slug
    # (e.g. a client mirroring a re-import), honour it.
    if not cir_data.get("id_slug"):
        from rrxiv.server.papers.slug import mint_slug

        if previous_version:
            prior = store.get_paper(previous_version)
            if prior is not None and prior.get("id_slug"):
                cir_data["id_slug"] = prior["id_slug"]
            else:
                cir_data["id_slug"] = mint_slug(store)
        else:
            cir_data["id_slug"] = mint_slug(store)

    # Compute revision_diff for revision submissions (RRP-0017). Done
    # *before* persisting so dry-runs see it too.
    revision_diff_payload: dict[str, Any] | None = None
    if previous_version:
        prior = store.get_paper(previous_version)
        if prior is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "previous_version_not_found",
                    "message": (
                        f"previous_version {previous_version!r} does not "
                        "exist in the corpus"
                    ),
                },
            )
        prior_cir_raw = store.get_cir(prior["id"]) or dict(prior)
        prior_cir = CIR.model_validate(prior_cir_raw)
        # The current paper's metadata for the diff: use what we'll
        # store, not what the store has (we haven't persisted yet).
        revision_diff_payload = compute_revision_diff(
            prior, prior_cir, cir_data, cir_obj
        )

    # ---- Dry-run short-circuit (RRP-0016) -------------------------
    if is_dry_run:
        return JSONResponse(
            status_code=200,
            content={
                "paper_id": None,
                "id_slug": cir_data.get("id_slug"),
                "version": cir_data.get("version"),
                "previous_version": cir_data.get("previous_version"),
                "revision_diff": revision_diff_payload,
                "would_persist": True,
                "dry_run": True,
            },
        )

    # Idempotency.
    if idempotency_key:
        existing = store.get_idempotency(auth.token, idempotency_key)
        if existing is not None:
            return JSONResponse(status_code=201, content=existing.response_body)

    paper_metadata = {
        k: v
        for k, v in cir_data.items()
        if k not in ("claims", "citations", "annotations", "sections", "figures")
    }
    store.add_paper(paper_metadata)
    store.add_cir(cir_data)

    # Persist each claim into the dedicated claims table so the
    # ``/papers/{id}/claims`` + ``/claims/{id}`` + claim-graph
    # endpoints find them. The seed-store flow already does this;
    # the submit flow previously stored only the bundled CIR blob
    # and the paper metadata, leaving the claims invisible to the
    # read paths even though they were inside the CIR. Surfaced
    # live on whitepaper v3 (paper page showed "0 claims").
    for claim in cir_data.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        # Ensure the claim's paper_id reflects the (possibly newly
        # minted) paper id, not whatever the client wrote.
        cleaned_claim = dict(claim)
        cleaned_claim["paper_id"] = paper_id
        store.add_claim(cleaned_claim)

    source_uri = store.save_source(paper_id, bundle_bytes)

    # Synthesise a revision_summary annotation if the submitter passed
    # `revision_summary` (RRP-0017). The author can supersede this with
    # a richer one later.
    if previous_version and revision_summary:
        _synthesise_revision_summary_annotation(
            store=store,
            new_paper_id=paper_id,
            previous_version_id=previous_version,
            summary=revision_summary,
            identity=auth.identity,
        )

    response_body = {
        "paper_id": paper_id,
        "id_slug": cir_data.get("id_slug"),
        "version": cir_data.get("version"),
        "previous_version": cir_data.get("previous_version"),
        "retrieval_uri": source_uri,
        "revision_diff": revision_diff_payload,
        "would_persist": True,
        "dry_run": False,
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

    return JSONResponse(status_code=201, content=response_body)


def _synthesise_revision_summary_annotation(
    *,
    store: Store,
    new_paper_id: str,
    previous_version_id: str,
    summary: str,
    identity: Any,
) -> None:
    """Create a skeleton ``revision_summary`` annotation on the v2
    paper (RRP-0017). Author can supersede later with structured
    highlights."""
    import datetime as dt

    # Identity → created_by — keep the existing shape conventions.
    if hasattr(identity, "orcid"):
        created_by = {"identity_type": "orcid", "identity": identity.orcid}
    elif hasattr(identity, "handle"):
        created_by = {"identity_type": "agent", "identity": identity.handle}
    else:
        # Fallback — should not happen given the auth gate above.
        created_by = {"identity_type": "agent", "identity": "unknown"}

    annotation = {
        "id": f"ann-{uuid.uuid4().hex[:10]}",
        "target_id": new_paper_id,
        "target_type": "paper",
        "annotation_type": "revision_summary",
        "content": summary,
        "structured_payload": {
            "previous_version_id": previous_version_id,
            "summary": summary,
            "highlights": [],
        },
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "created_by": created_by,
    }
    store.add_annotation(annotation)


def _mint_paper_id() -> str:
    return f"paper-{uuid.uuid4().hex[:12]}"


# ---- Source download + versions live on /papers/{id}/* ----


sources_router = APIRouter(prefix="/papers", tags=["Papers"])


def _resolve(paper_id: str, store: Store) -> dict[str, Any] | None:
    """Resolve a paper by canonical id or slug. Inline-imported so the
    submissions module doesn't pull the slug module at import time."""
    from rrxiv.server.papers.slug import find_paper_by_slug, is_slug

    if is_slug(paper_id):
        return find_paper_by_slug(store, paper_id)
    return store.get_paper(paper_id)


@sources_router.get("/{paper_id}/source")
def get_paper_source(paper_id: str, request: Request) -> Response:
    """Stream a paper's source archive."""
    store: Store = get_store(request)
    paper = _resolve(paper_id, store)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]
    blob = store.load_source(canonical_id)
    if blob is None:
        raise not_found(f"paper {paper_id} has no source archive")
    filename = paper.get("id_slug") or canonical_id
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.tar.gz"'
        },
    )


@sources_router.get("/{paper_id}/source/manifest")
def get_paper_source_manifest(paper_id: str, request: Request) -> dict[str, Any]:
    """List the files inside the source archive.

    Returns ``{"files": [{"path": ..., "size": ..., "kind": ...}]}``.
    ``kind`` is one of ``tex``, ``cls``, ``sty``, ``bib``, ``image``,
    ``other`` — the web client uses it to pick a syntax highlighter
    and decide whether to render the file inline.
    """
    import io
    import tarfile

    store: Store = get_store(request)
    paper = _resolve(paper_id, store)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]
    blob = store.load_source(canonical_id)
    if blob is None:
        raise not_found(f"paper {paper_id} has no source archive")

    files: list[dict[str, Any]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                if _is_noise_file(member.name):
                    continue
                files.append(
                    {
                        "path": member.name,
                        "size": member.size,
                        "kind": _classify_source_file(member.name),
                    }
                )
    except tarfile.TarError as e:
        raise not_found(
            f"paper {paper_id} source archive is unreadable: {e}"
        ) from e

    files.sort(key=lambda f: (_source_sort_key(f["path"]), f["path"]))
    return {"paper_id": canonical_id, "files": files}


@sources_router.get("/{paper_id}/source/file")
def get_paper_source_file(
    paper_id: str, path: str, request: Request
) -> Response:
    """Stream one file from the source archive as UTF-8 text.

    ``path`` is a tar member name from ``/source/manifest``. The
    response is ``text/plain`` for source files (.tex, .cls, .sty,
    .bib) and ``application/octet-stream`` for everything else (the
    client uses the manifest to decide what to render).
    """
    import io
    import tarfile

    store: Store = get_store(request)
    paper = _resolve(paper_id, store)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]
    blob = store.load_source(canonical_id)
    if blob is None:
        raise not_found(f"paper {paper_id} has no source archive")

    # Defensive: tarball member names should be relative, but reject
    # anything that tries to traverse via "..".
    if ".." in path.split("/"):
        raise not_found(f"path {path!r} is not in the archive")

    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            try:
                member = tar.getmember(path)
            except KeyError as e:
                raise not_found(
                    f"path {path!r} is not in the archive"
                ) from e
            if not member.isfile():
                raise not_found(f"path {path!r} is not a regular file")
            handle = tar.extractfile(member)
            if handle is None:
                raise not_found(f"path {path!r} could not be read")
            content = handle.read()
    except tarfile.TarError as e:
        raise not_found(
            f"paper {paper_id} source archive is unreadable: {e}"
        ) from e

    kind = _classify_source_file(path)
    if kind in {"tex", "cls", "sty", "bib", "other"} and len(content) < 5_000_000:
        # Render as text if it's plausibly a source file.
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return Response(
                content=content, media_type="application/octet-stream"
            )
        return Response(content=text, media_type="text/plain; charset=utf-8")
    return Response(content=content, media_type="application/octet-stream")


_SOURCE_KIND_BY_EXT: dict[str, str] = {
    ".tex": "tex",
    ".cls": "cls",
    ".sty": "sty",
    ".bib": "bib",
    ".bbl": "bib",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".pdf": "image",
    ".svg": "image",
}


def _classify_source_file(path: str) -> str:
    """Bucket a tar member name into a UI kind."""
    lower = path.lower()
    for ext, kind in _SOURCE_KIND_BY_EXT.items():
        if lower.endswith(ext):
            return kind
    return "other"


def _is_noise_file(path: str) -> bool:
    """Filter macOS apple-double / Spotlight / git noise from listings.

    The tarball produced on macOS by ``tar -czf`` includes ``._foo`` and
    ``.DS_Store`` siblings; they're never useful to readers.
    """
    base = path.rsplit("/", 1)[-1]
    if base.startswith("._"):
        return True
    if base in {".DS_Store", "Thumbs.db", ".gitkeep"}:
        return True
    if "/__MACOSX/" in f"/{path}/":
        return True
    return False


def _source_sort_key(path: str) -> int:
    """Order: main.tex first, then *.tex, then everything else.

    Web client opens the first entry by default; surface the
    plausibly-relevant file at the top.
    """
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    if base in {"main.tex", "main-flat.tex", "paper.tex"}:
        return 0
    if lower.endswith(".tex"):
        return 1
    if lower.endswith((".cls", ".sty")):
        return 2
    if lower.endswith(".bib"):
        return 3
    return 4


@sources_router.get("/{paper_id}/pdf")
def get_paper_pdf(paper_id: str, request: Request) -> Response:
    """Stream a paper's compiled PDF, if the server has one bundled."""
    store: Store = get_store(request)
    paper = _resolve(paper_id, store)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]
    blob = store.load_rendered_pdf(canonical_id)
    if blob is None:
        raise not_found(f"paper {paper_id} has no rendered PDF")
    filename = paper.get("id_slug") or canonical_id
    return Response(
        content=blob,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}.pdf"',
            "Cache-Control": "public, max-age=3600",
        },
    )


@sources_router.get("/{paper_id}/versions")
def get_paper_versions(paper_id: str, request: Request) -> dict[str, Any]:
    """Return the chain of versions for ``paper_id`` ordered oldest-first.

    Walks ``previous_version`` pointers in the in-memory paper records.
    Cycle-safe: tracks visited ids so a self-referential or cyclic
    ``previous_version`` chain (which a buggy submit-flow could
    produce) returns the truncated chain instead of looping forever.
    """
    store: Store = get_store(request)
    paper = store.get_paper(paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")

    # Walk backward via previous_version. Bounded by `visited` so a
    # self-loop (paper.previous_version == paper.id) or an arbitrary
    # cycle terminates after one pass through each node.
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    cur: dict[str, Any] | None = paper
    while cur is not None:
        cur_id = cur.get("id")
        if not cur_id or cur_id in visited:
            break
        visited.add(cur_id)
        chain.append(
            {
                "id": cur_id,
                "version": cur.get("version"),
                "submitted_at": cur.get("submitted_at"),
                "previous_version": cur.get("previous_version"),
            }
        )
        prev_id = cur.get("previous_version")
        if not prev_id or prev_id == cur_id:
            break
        cur = store.get_paper(prev_id)
    chain.reverse()
    return {"items": chain}
