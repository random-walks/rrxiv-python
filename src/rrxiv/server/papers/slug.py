"""Slug minting + parsing for paper id_slug (RRP-0013).

Paper IDs are UUIDv7 — collision-free and machine-friendly, but ugly in
URLs and citations. RRP-0013 adds an `id_slug` field of the form
`rrxiv:YYMM.NNNNN`, arXiv-shaped, server-minted at submission. This
module owns the minting algorithm and the lookup-by-slug helper.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rrxiv.server.store import Store

SLUG_PATTERN = re.compile(r"^rrxiv:(\d{4})\.(\d{5})$")
"""Match the canonical rrxiv:YYMM.NNNNN form. Group 1: YYMM. Group 2: NNNNN."""


def is_slug(s: str) -> bool:
    """True if ``s`` looks like a paper slug (`rrxiv:YYMM.NNNNN`)."""
    return SLUG_PATTERN.match(s) is not None


def claim_owner_key(paper: dict[str, Any]) -> str:
    """The identity a paper's claims + annotations are keyed off.

    Claim ids are citable + slug-based: ``claim.id`` is
    ``<id_slug>:<local_label>`` and ``claim.paper_id`` is the owning
    paper's ``id_slug`` (RRP-0013, RRP-0029) — built client-side at
    authoring time, before the server mints the opaque machine ``id``
    (a UUIDv7). So every "which claims/annotations belong to this
    paper?" filter must key off the slug, NOT ``paper["id"]``.

    Falls back to ``paper["id"]`` for legacy/degenerate records that
    never got a slug (and for the historical corpus where ``id`` and
    ``id_slug`` were the same string).
    """
    return str(paper.get("id_slug") or paper["id"])


def slug_yymm(now: datetime | None = None) -> str:
    """Return the YYMM segment for the given (UTC) timestamp; default: now()."""
    when = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    return f"{when.year % 100:02d}{when.month:02d}"


def mint_slug(store: Store, submitted_at: datetime | None = None) -> str:
    """Mint the next slug for the (YY,MM) of ``submitted_at`` (default: now).

    Algorithm: scan all existing papers' ``id_slug`` fields, find the
    highest counter for the current YYMM, return that + 1 zero-padded
    to 5 digits.

    O(N) over the corpus. For v0.1's 10-paper seed this is irrelevant;
    when the corpus grows, swap the store implementation for one that
    maintains a per-month counter.
    """
    yymm = slug_yymm(submitted_at)
    max_counter = 0
    for paper in store.list_papers():
        existing = paper.get("id_slug")
        if not existing:
            continue
        m = SLUG_PATTERN.match(existing)
        if m is None:
            continue
        existing_yymm, existing_n = m.group(1), m.group(2)
        if existing_yymm != yymm:
            continue
        n = int(existing_n)
        if n > max_counter:
            max_counter = n
    return f"rrxiv:{yymm}.{(max_counter + 1):05d}"


def find_paper_by_slug(store: Store, slug: str) -> dict[str, Any] | None:
    """Linear scan for the head-of-lineage paper matching ``id_slug``.

    Slugs are stable across revisions (RRP-0013), so multiple paper
    rows can share a slug — one per version in the lineage chain.
    The "right" paper to return is the HEAD (latest version, i.e.
    the one nothing else points to via ``previous_version``).

    Earlier this function returned the first match in insertion
    order, which surfaced v1 instead of v4 on the paper-detail page
    after a revision landed. Now we collect all candidates, drop
    those that any other candidate supersedes, and return the
    remaining one. If for some reason multiple "heads" exist
    (concurrent forks, data corruption), we fall back to the most
    recently submitted.

    Default implementation suitable for any ``Store``. SQL-backed
    stores can override with an indexed lookup once the corpus
    warrants it.
    """
    candidates = [p for p in store.list_papers() if p.get("id_slug") == slug]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Drop any candidate that is the previous_version of another
    # candidate in the same slug-set (older versions).
    superseded_ids = {
        p.get("previous_version")
        for p in candidates
        if p.get("previous_version")
    }
    heads = [p for p in candidates if p.get("id") not in superseded_ids]

    if len(heads) == 1:
        return heads[0]
    if not heads:
        # No clear head (cycle, perhaps) — fall back to newest.
        return max(
            candidates,
            key=lambda p: p.get("submitted_at") or "",
        )
    # Multiple heads (concurrent forks) — return the most recent.
    return max(heads, key=lambda p: p.get("submitted_at") or "")
