"""Canonicalise a CIR's claim ids / ``paper_id`` / edge targets /
annotation target_ids to the paper's citable ``id_slug``.

Shared by the seed-store path (``cli/seed.py``) and the submission path
(``server/submissions/router.py``) so both key claims off the slug
identically (RRP-0013 / RRP-0029).
"""

from __future__ import annotations

from typing import Any


def canonicalise_claim_ids(cir: dict[str, Any], owner_slug: str) -> int:
    """Rewrite the CIR's OWN-paper claim-id / ``paper_id`` / edge / annotation
    prefixes from the parser's build-time meta-slug to the paper's citable
    ``id_slug`` (``owner_slug``).

    Claim ids are citable + slug-based: ``claim.id`` is
    ``<id_slug>:<local_label>`` and ``claim.paper_id`` is the owning paper's
    ``id_slug`` (RRP-0013, RRP-0029) — NOT the opaque machine ``id`` (a
    UUIDv7), which the server mints only at submission and the read paths use
    solely as a storage PK.

    ``rrxiv parse`` stamps ids as ``<meta_slug>:<kind>:<label>`` using the
    paper repo's ``rrxiv-meta.json`` slug (e.g. ``rrxiv-paper-euclid-elements``)
    or a placeholder for a brand-new paper — neither is the citable
    ``rrxiv:YYMM.NNNNN`` slug resolved at seed/submit time. We substitute every
    local parser-prefix occurrence so the deployed instance finds the claims
    via the slug-keyed filter (``claim_owner_key`` -> ``list_claims_for_paper``).

    Only the OWN-paper prefix is rewritten; cross-paper edges (``depends_on`` /
    ``supports`` / ``contradicts`` / ``extends``) that reference OTHER papers'
    claim ids are left untouched. Idempotent: a no-op when the ids already use
    ``owner_slug`` as the prefix.

    Returns the number of substitutions made.
    """
    claims = cir.get("claims") or []
    if not claims:
        return 0

    # All claims from one parse run share a prefix; peek at the first.
    sample_id = claims[0].get("id") or ""

    # Already-canonical fast path. The target prefix (the id_slug) itself
    # contains a colon (``rrxiv:YYMM.NNNNN``), so a naive split(":", 1) would
    # mis-read an already-canonical id like ``rrxiv:2605.00099:claim:c1`` as
    # prefix ``rrxiv`` and double the slug. Guard it.
    if sample_id.startswith(owner_slug + ":"):
        return 0

    # The parser's meta_slug is colon-free (e.g. ``rrxiv-paper-euclid-elements``),
    # so the prefix to replace is everything up to the first colon.
    parts = sample_id.split(":", 1)
    if len(parts) != 2:
        return 0
    parser_prefix = parts[0]
    if parser_prefix == owner_slug:
        return 0

    old = parser_prefix + ":"
    new = owner_slug + ":"

    def _rewrite(s: str) -> str:
        return new + s[len(old) :] if s.startswith(old) else s

    n = 0
    for c in claims:
        if (cid := c.get("id")) and cid.startswith(old):
            c["id"] = _rewrite(cid)
            n += 1
        if c.get("paper_id") != owner_slug:
            c["paper_id"] = owner_slug
            n += 1
        for key in ("depends_on", "supports", "contradicts", "extends"):
            edges = c.get(key)
            if not edges:
                continue
            new_edges = [_rewrite(t) for t in edges]
            if new_edges != edges:
                c[key] = new_edges
                n += sum(
                    1 for a, b in zip(edges, new_edges, strict=True) if a != b
                )

    # Annotations targeting this paper's claims need rewriting too;
    # cross-paper targets are safe (only the local prefix is touched).
    for ann in cir.get("annotations") or []:
        if (tid := ann.get("target_id")) and tid.startswith(old):
            ann["target_id"] = _rewrite(tid)
            n += 1

    return n
