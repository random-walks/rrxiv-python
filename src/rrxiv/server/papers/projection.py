"""View-model projection for the papers endpoints (RRP-0012).

The canonical ``Paper`` is immutable post-submission, but list-view UIs
want aggregate counts (claims, replicated, contradicted, contested,
paper-level status). These are *derived* from the corpus state, not
authored, and change as annotations land.

``compute_stats`` and ``to_list_item`` produce the wire shape for
``GET /api/v0/papers`` and ``GET /api/v0/papers/{id}?include=stats``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rrxiv.server.claims.replication import derive_replication_status
from rrxiv.server.papers.slug import claim_owner_key

if TYPE_CHECKING:
    from rrxiv.server.store import Store


def compute_stats(owner_key: str, store: Store) -> dict[str, Any]:
    """Compute aggregate stats for a paper from claims + annotations.

    ``owner_key`` is the value claims + paper-level annotations are
    keyed off — the paper's citable ``id_slug`` (RRP-0013 / RRP-0029),
    NOT its opaque machine ``id`` (a UUIDv7). Callers should pass
    ``claim_owner_key(paper)``. (Claim ids are ``<id_slug>:<label>`` and
    ``claim.paper_id`` is the ``id_slug``; for the legacy corpus where
    ``id == id_slug`` these coincide.)

    Returns a dict matching ``paper_list_item.schema.json#/$defs/Stats``::

        {
          "claims": int,
          "replicated": int,
          "partial": int,
          "contradicted": int,
          "contested": int,
          "untested": int,
          "status": "preprint" | "untested" | "partial" | "replicated"
                    | "contested" | "retracted",
          "computed_at": ISO-8601 datetime
        }

    The per-claim ``replication_status`` enum (RRP-0019) is
    ``{untested, partial, replicated, contradicted, retracted}`` —
    note there is no ``contested`` at the claim level. ``partial``
    means "has supporting replications below the discipline quorum"
    and is distinct from ``contested`` ("the corpus contains a
    genuine mix of supports and contradicts").

    Earlier versions of this projection conflated ``partial`` claims
    into the ``contested`` count, so a paper with N partials and 0
    contradicts surfaced as ``status='contested'`` on the home page
    even though no one had filed a contradiction. ``partial`` now
    has its own bucket; the paper-level ``contested`` status only
    fires when both a replication and a contradiction landed
    somewhere in the paper.

    Per-claim ``replication_status`` is **derived server-side** from
    annotations (RRP-0019, RRP-0020) — the value persisted on the
    claim is advisory only. See RRP-0012 for the paper-level status
    rollup rules.
    """
    # Claim aggregates ---------------------------------------------------
    claims = [c for c in store.list_claims() if c.get("paper_id") == owner_key]
    replicated = 0
    partial = 0
    contradicted = 0
    retracted_claims = 0
    untested = 0
    for claim in claims:
        cid = claim.get("id")
        rs = (
            derive_replication_status(
                cid, store, authored_default=claim.get("replication_status")
            )
            if cid
            else "untested"
        )
        if rs == "replicated":
            replicated += 1
        elif rs == "partial":
            partial += 1
        elif rs == "contradicted":
            contradicted += 1
        elif rs == "retracted":
            retracted_claims += 1
        else:
            untested += 1

    # Annotations ---------------------------------------------------------
    paper_annotations = [
        a for a in store.list_annotations() if a.get("target_id") == owner_key
    ]
    is_retracted = any(
        a.get("annotation_type") == "erratum"
        and isinstance(a.get("structured_payload"), dict)
        and a["structured_payload"].get("retracted") is True
        for a in paper_annotations
    )
    has_any_annotation = bool(paper_annotations) or any(
        a.get("target_id", "").startswith(f"{owner_key}:")
        for a in store.list_annotations()
    )

    # Paper-level status rollup -------------------------------------------
    #
    # Precedence (highest first):
    #   1. retracted   — paper-level retraction annotation
    #   2. contested   — genuine mixed evidence (at least one
    #                    contradiction AND at least one replication)
    #   3. contradicted — contradictions exist, no replication landed yet
    #   4. replicated  — at least one fully-replicated claim, no
    #                    contradictions
    #   5. partial     — supports below quorum, no contradictions
    #   6. untested    — claims exist, some discourse but no replication
    #                    evidence
    #   7. preprint    — no claims OR no annotations at all
    if is_retracted:
        status = "retracted"
    elif len(claims) == 0 or (
        replicated == 0
        and partial == 0
        and contradicted == 0
        and not has_any_annotation
    ):
        status = "preprint"
    elif contradicted > 0 and replicated > 0:
        status = "contested"
    elif contradicted > 0:
        status = "contradicted"
    elif replicated > 0:
        status = "replicated"
    elif partial > 0:
        status = "partial"
    else:
        # Claims exist, no replication evidence yet, but the discourse
        # is non-empty (comments, summaries, extensions).
        status = "untested"

    # ``contested`` count: number of claims with mixed evidence on the
    # claim itself. Per-claim derivation doesn't surface a "contested"
    # state currently (claims resolve to contradicted / replicated /
    # partial), so this stays 0 unless future schema work splits it
    # out. Kept in the output for forward-compat + so the wire shape
    # doesn't break existing consumers.
    contested = 0

    return {
        "claims": len(claims),
        "replicated": replicated,
        "partial": partial,
        "contradicted": contradicted,
        "contested": contested,
        "untested": untested,
        "status": status,
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def to_list_item(paper: dict[str, Any], store: Store) -> dict[str, Any]:
    """Combine a Paper with its computed Stats into a PaperListItem."""
    stats = compute_stats(claim_owner_key(paper), store)
    item = dict(paper)
    item["stats"] = stats
    return item
