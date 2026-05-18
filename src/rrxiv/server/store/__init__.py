"""Storage protocol + factory.

Per RRP-0008 / RRP-0011: the reference server picks one of two
backends at boot:

- :class:`MemoryStore` (default; ephemeral)
- :class:`SqliteStore` (persistent; configured via
  ``RRXIV_STORE_URL=sqlite:///path/to/db.sqlite``)

Future RRPs add Postgres etc. without router changes.
"""

from __future__ import annotations

from rrxiv.server.store.memory import MemoryStore
from rrxiv.server.store.protocol import (
    AgentIdentity,
    AgentRecord,
    AnonymousIdentity,
    IdempotencyEntry,
    Identity,
    OrcidIdentity,
    Store,
    TokenRecord,
)
from rrxiv.server.store.sqlite import SqliteStore, parse_store_url


def store_from_url(url: str) -> Store:
    """Construct a Store from a ``store_url`` setting.

    ``memory://`` (the default) → :class:`MemoryStore`.
    ``sqlite:///<path>`` (or ``sqlite:///:memory:``) → :class:`SqliteStore`.
    Anything else raises :class:`ValueError`.
    """
    if url == "memory://":
        return MemoryStore()
    sqlite_path = parse_store_url(url)
    if sqlite_path is not None:
        return SqliteStore(sqlite_path)
    raise ValueError(f"unknown store_url scheme: {url!r}")


__all__ = [
    "AgentIdentity",
    "AgentRecord",
    "AnonymousIdentity",
    "IdempotencyEntry",
    "Identity",
    "MemoryStore",
    "OrcidIdentity",
    "SqliteStore",
    "Store",
    "TokenRecord",
    "store_from_url",
]
