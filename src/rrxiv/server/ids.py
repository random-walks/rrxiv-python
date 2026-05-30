"""Server-side identifier minting.

The canonical machine ``id`` for a paper (and any other server-minted
record that wants time-ordered, collision-resistant keys) is a
**UUIDv7** per RFC 9562 §5.7 — a 48-bit Unix-millisecond timestamp in
the most-significant bits, the 4-bit version (``0b0111``), a 2-bit
variant (``0b10``), and 74 bits of randomness. The leading timestamp
makes freshly-minted ids monotonically sortable (handy as a storage
PK + for "newest first" ordering) while the random tail keeps them
opaque and unguessable. Per RRP-0029 this is the paper's opaque
machine ``id``; humans cite the ``id_slug`` instead.

Python 3.11 has no ``uuid.uuid7`` (added in 3.14), so we synthesise it
here. The layout follows RFC 9562 exactly:

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                           unix_ts_ms                          |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |          unix_ts_ms           |  ver  |       rand_a          |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |var|                        rand_b                            |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                            rand_b                            |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Mint a version-7 UUID (RFC 9562 §5.7).

    The 48 most-significant bits carry the current Unix time in
    milliseconds, so the textual form is monotonically increasing for
    ids minted in different milliseconds. Returns a standard
    ``uuid.UUID`` whose ``.version`` is 7 and whose variant is the
    RFC 4122 / 9562 ``0b10``.
    """
    ms = time.time_ns() // 1_000_000
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    n = (ms & ((1 << 48) - 1)) << 80
    n |= 0x7 << 76  # version 7
    n |= rand_a << 64
    n |= 0b10 << 62  # variant 0b10
    n |= rand_b
    return uuid.UUID(int=n)
