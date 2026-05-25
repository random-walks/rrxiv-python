"""Tiny TTL cache for ``compute_pulse``.

The pulse computation is O(corpus); at the current ~9 papers + ~70
annotations scale it runs in <5ms. Wrapping it in a 60s wall-clock
cache prevents a "/pulse refresh storm" from a dashboard widget
hitting the endpoint every page load.

Not thread-safe in the strict sense, but the consequence of a race
is at worst one extra recompute — never stale data, never corruption.
"""

from __future__ import annotations

import time
from typing import Any

_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_TTL_SECONDS = 60.0


def get_or_compute(
    key: tuple[Any, ...],
    factory: Any,
    *,
    ttl_seconds: float = _TTL_SECONDS,
) -> dict[str, Any]:
    """Return the cached value for ``key`` or recompute via
    ``factory()``. ``factory`` is called with no arguments.
    """
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None:
        ts, value = cached
        if now - ts < ttl_seconds:
            return value
    fresh: dict[str, Any] = factory()
    _CACHE[key] = (now, fresh)
    return fresh


def invalidate() -> None:
    """Clear all cached entries — useful for tests."""
    _CACHE.clear()
