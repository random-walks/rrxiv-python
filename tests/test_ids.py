"""Tests for server-side id minting (``rrxiv.server.ids``) + the paper
id mint in the submissions router.

Per RRP-0029, ``paper.id`` is a server-minted UUIDv7 (the opaque
machine id / storage PK); humans cite the slug-based ``id_slug``.
"""

from __future__ import annotations

import re
import uuid

from rrxiv.server.ids import uuid7
from rrxiv.server.submissions.router import _mint_paper_id


def test_uuid7_returns_version_7_uuid() -> None:
    u = uuid7()
    assert isinstance(u, uuid.UUID)
    assert u.version == 7
    # RFC 9562 / 4122 variant is 0b10 — `uuid` exposes this as RFC_4122.
    assert u.variant == uuid.RFC_4122


def test_uuid7_unique_and_time_ordered() -> None:
    # 100 calls: all distinct.
    minted = [uuid7() for _ in range(100)]
    assert len({str(u) for u in minted}) == 100

    # Roughly timestamp-ordered: the 48-bit MSB millisecond prefix is
    # monotonic, so the integer values are non-decreasing across calls
    # (ties only possible within the same millisecond, where the random
    # tail can reorder). Compare the timestamp prefix only — it must be
    # non-decreasing.
    def ts_ms(u: uuid.UUID) -> int:
        return u.int >> 80

    prefixes = [ts_ms(u) for u in minted]
    assert prefixes == sorted(prefixes)


def test_mint_paper_id_is_canonical_uuid_string() -> None:
    pid = _mint_paper_id()
    # 36-char canonical UUID, NOT the old ``paper-<12hex>`` shape.
    assert re.fullmatch(r"^[0-9a-f-]{36}$", pid), pid
    assert not pid.startswith("paper-")
    # Round-trips through uuid.UUID and is version 7.
    parsed = uuid.UUID(pid)
    assert parsed.version == 7
