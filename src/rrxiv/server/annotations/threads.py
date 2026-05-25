"""Helpers for annotation threads (RRP-0018).

An annotation may carry ``in_reply_to: <annotation_id>``. The server
validates the pointer at write time and exposes a convenience reverse
lookup endpoint for fast thread reconstruction on the read side.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from rrxiv.server.store import Store


def _paper_id_of_target(target_id: str, target_type: str | None) -> str | None:
    """Extract the owning paper_id for a target.

    - ``paper`` targets: the target_id itself is the paper_id.
    - ``claim`` / ``section`` / ``figure`` targets: paper_id is the
      prefix before the first colon.
    - ``annotation`` targets: cannot be derived from target_id alone; the
      caller resolves the annotation and re-applies this function to
      whatever that annotation pointed at.
    """
    if not isinstance(target_id, str):
        return None
    if target_type == "paper":
        return target_id
    if ":" in target_id:
        return target_id.split(":", 1)[0]
    return None


def validate_in_reply_to(
    store: Store,
    body: dict[str, Any],
) -> None:
    """Raise HTTPException(400) if ``body.in_reply_to`` is malformed.

    Rules per RRP-0018:
      1. Target exists.
      2. Same artefact: same paper, and when both annotations are
         claim-targeted, the same claim.
      3. Not self-reply.
    """
    target_id = body.get("in_reply_to")
    if not target_id:
        return  # Optional field; absent or null is fine.

    target = store.get_annotation(target_id)
    if target is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "in_reply_to_not_found",
                "message": f"annotation {target_id!r} does not exist",
            },
        )

    if body.get("id") and body["id"] == target_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "in_reply_to_self",
                "message": "annotation cannot reply to itself",
            },
        )

    new_paper = _paper_id_of_target(
        str(body.get("target_id") or ""), str(body.get("target_type") or "")
    )
    old_paper = _paper_id_of_target(
        str(target.get("target_id") or ""), str(target.get("target_type") or "")
    )
    if new_paper and old_paper and new_paper != old_paper:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "in_reply_to_artefact_mismatch",
                "message": (
                    f"in_reply_to target lives on paper {old_paper!r} but "
                    f"this annotation targets paper {new_paper!r}"
                ),
            },
        )

    # When both target a claim, the claim_id must match exactly.
    if (
        body.get("target_type") == "claim"
        and target.get("target_type") == "claim"
        and body.get("target_id") != target.get("target_id")
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "in_reply_to_artefact_mismatch",
                "message": (
                    f"in_reply_to target lives on claim "
                    f"{target.get('target_id')!r} but this annotation "
                    f"targets claim {body.get('target_id')!r}"
                ),
            },
        )


def list_direct_replies(store: Store, ann_id: str) -> list[dict[str, Any]]:
    """Return annotations whose ``in_reply_to`` equals ``ann_id``, sorted
    oldest-first."""
    out = [
        a for a in store.list_annotations() if a.get("in_reply_to") == ann_id
    ]
    out.sort(key=lambda a: (a.get("created_at") or "", a.get("id") or ""))
    return out
