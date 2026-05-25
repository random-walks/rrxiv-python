"""``GET /stats/pulse`` — community + protocol KPI snapshot.

Public read, no auth. Cached 60s in-process so a dashboard refresh
storm doesn't keep recomputing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_settings, get_store
from rrxiv.server.stats.cache import get_or_compute
from rrxiv.server.stats.pulse import compute_pulse, parse_window
from rrxiv.server.store import Store

router = APIRouter(tags=["Stats"])


@router.get("/stats/pulse")
def stats_pulse(
    request: Request,
    window: str = Query("7d", description="7d | 30d | 90d | all"),
) -> dict[str, Any]:
    """Activity + health + growth aggregates for the canonical
    dashboard.

    Self-exclusion: identities listed in
    ``ServerSettings.exclude_identities`` (env: ``RRXIV_EXCLUDE_IDENTITIES``,
    comma-separated) are dropped from the activity counts so the
    dashboard reflects *real community* participation, not the
    maintainer's dogfooding.

    The full response shape is RRP-0022 / ``pulse_snapshot.schema.json``.
    """
    store: Store = get_store(request)
    settings = get_settings(request)
    parsed = parse_window(window)
    exclude = set(settings.exclude_identities or [])

    # Cache key includes the corpus length so a fresh submission
    # invalidates without waiting 60s for the TTL — cheap heuristic.
    paper_n = len(list(store.list_papers()))
    ann_n = len(list(store.list_annotations()))
    key = ("pulse", parsed, frozenset(exclude), paper_n, ann_n)

    # Sprint 21: histogram the compute time so we can spot the cliff
    # if the corpus grows past what the in-process aggregator can
    # sustainably handle. Cache hits skip the timer.
    def _compute_with_timing() -> dict[str, Any]:
        try:
            from rrxiv.server.metrics import pulse_compute_seconds

            with pulse_compute_seconds.time():
                return compute_pulse(
                    store, window=parsed, exclude_identities=exclude
                )
        except Exception:
            return compute_pulse(
                store, window=parsed, exclude_identities=exclude
            )

    return get_or_compute(key, _compute_with_timing)
