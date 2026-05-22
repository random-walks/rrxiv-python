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

if TYPE_CHECKING:
    from rrxiv.server.store import Store


def compute_stats(paper_id: str, store: Store) -> dict[str, Any]:
    """Compute aggregate stats for a paper from claims + annotations.

    Returns a dict matching ``paper_list_item.schema.json#/$defs/Stats``::

        {
          "claims": int,
          "replicated": int,
          "contradicted": int,
          "contested": int,
          "untested": int,
          "status": "preprint" | "untested" | "replicated" | "contested" | "retracted",
          "computed_at": ISO-8601 datetime
        }

    Per-claim ``replication_status`` is **derived server-side** from
    annotations (RRP-0019, RRP-0020) — the value persisted on the claim
    is advisory only. See RRP-0012 for the paper-level status rollup
    rules.
    """
    # Claim aggregates ---------------------------------------------------
    claims = [c for c in store.list_claims() if c.get("paper_id") == paper_id]
    replicated = 0
    contradicted = 0
    contested = 0
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
        elif rs == "contradicted":
            contradicted += 1
        elif rs == "partial":
            contested += 1
        else:
            # "untested" / "retracted" / unset
            untested += 1

    # Annotations ---------------------------------------------------------
    paper_annotations = [
        a for a in store.list_annotations() if a.get("target_id") == paper_id
    ]
    is_retracted = any(
        a.get("annotation_type") == "erratum"
        and isinstance(a.get("structured_payload"), dict)
        and a["structured_payload"].get("retracted") is True
        for a in paper_annotations
    )
    has_any_annotation = bool(paper_annotations) or any(
        a.get("target_id", "").startswith(f"{paper_id}:")
        for a in store.list_annotations()
    )

    # Paper-level status rollup -------------------------------------------
    if is_retracted:
        status = "retracted"
    elif (
        len(claims) == 0
        or (replicated == 0 and contradicted == 0 and contested == 0 and not has_any_annotation)
    ):
        status = "preprint"
    elif replicated == 0 and contradicted == 0 and contested == 0:
        # Claims exist, all untested, but annotations have been filed —
        # the discourse is non-empty even if nothing is replicated yet.
        status = "untested"
    elif replicated > 0 and contradicted == 0 and contested == 0:
        status = "replicated"
    else:
        # Any mix of replicated + non-replicated evidence, or
        # contradictions without replications.
        status = "contested"

    return {
        "claims": len(claims),
        "replicated": replicated,
        "contradicted": contradicted,
        "contested": contested,
        "untested": untested,
        "status": status,
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def to_list_item(paper: dict[str, Any], store: Store) -> dict[str, Any]:
    """Combine a Paper with its computed Stats into a PaperListItem."""
    stats = compute_stats(paper["id"], store)
    item = dict(paper)
    item["stats"] = stats
    return item
