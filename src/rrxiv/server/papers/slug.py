"""Slug minting + parsing for paper id_slug (RRP-0013).

Paper IDs are UUIDv7 — collision-free and machine-friendly, but ugly in
URLs and citations. RRP-0013 adds an `id_slug` field of the form
`rrxiv:YYMM.NNNNN`, arXiv-shaped, server-minted at submission. This
module owns the minting algorithm and the lookup-by-slug helper.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rrxiv.server.store import Store

SLUG_PATTERN = re.compile(r"^rrxiv:(\d{4})\.(\d{5})$")
"""Match the canonical rrxiv:YYMM.NNNNN form. Group 1: YYMM. Group 2: NNNNN."""


def is_slug(s: str) -> bool:
    """True if ``s`` looks like a paper slug (`rrxiv:YYMM.NNNNN`)."""
    return SLUG_PATTERN.match(s) is not None


def slug_yymm(now: datetime | None = None) -> str:
    """Return the YYMM segment for the given (UTC) timestamp; default: now()."""
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
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


def find_paper_by_slug(store: Store, slug: str) -> dict | None:
    """Linear scan for a paper with the given ``id_slug``.

    Default implementation suitable for any ``Store``. SQL-backed stores
    can override with an indexed lookup once the corpus warrants it.
    """
    for paper in store.list_papers():
        if paper.get("id_slug") == slug:
            return paper
    return None
