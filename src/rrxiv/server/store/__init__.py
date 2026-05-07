"""Storage protocol + factory.

The reference server is in-memory only in v0.1. Future RRPs (e.g.
SQLite, Postgres) provide alternative ``Store`` implementations
that the same routers depend on.
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

__all__ = [
    "AgentIdentity",
    "AgentRecord",
    "AnonymousIdentity",
    "IdempotencyEntry",
    "Identity",
    "MemoryStore",
    "OrcidIdentity",
    "Store",
    "TokenRecord",
]
