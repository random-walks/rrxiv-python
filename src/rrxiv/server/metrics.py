"""Prometheus counters + histograms for operational observability.

Sprint 21 — pairs with Fly's built-in `[metrics]` Prometheus scrape so
the Grafana dashboard at fly.io/apps/rrxiv-api/metrics gets real
request-rate + per-action numbers instead of just the platform-level
CPU / memory / network bands.

Design notes:

- **`/metrics`** is mounted at the root, *outside* `/api/v0`. It's an
  operational concern, not part of the protocol surface that
  third-party rrxiv implementations need to honour.

- **Cardinality is bounded.** We deliberately do NOT include
  `path` (per-paper-id) or `auth_id` (per-orcid) as labels. Path
  becomes `path_pattern` (FastAPI's route template) so the label
  space stays at ~30 distinct values. Auth becomes `auth_kind`
  (orcid|agent|anonymous|none) — 4 values.

- **Soft dep.** The module imports prometheus-client unconditionally,
  but if for some reason it's missing we fall back to no-op stubs so
  the server still boots. Same posture as sentry-sdk.

- **Pulse duration histogram** uses default buckets; the pulse
  endpoint should land < 100ms even with a 10k-annotation corpus,
  but the histogram lets us see when that stops being true.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import Response

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )

    _HAVE_PROM = True
except ImportError:  # pragma: no cover - extras guard

    class _NoOp:
        """Minimal stub so calls don't crash when prom-client is missing."""

        def labels(self, **_kwargs: Any) -> _NoOp:
            return self

        def inc(self, _amount: float = 1.0) -> None:
            return None

        def observe(self, _value: float) -> None:
            return None

    Counter = Histogram = _NoOp  # type: ignore[assignment,misc]
    CONTENT_TYPE_LATEST = "text/plain"
    _HAVE_PROM = False

    def generate_latest(*args: Any, **kwargs: Any) -> bytes:  # type: ignore[misc]
        return b"# prometheus-client not installed\n"


__all__ = [
    "annotations_posted_total",
    "http_requests_total",
    "metrics_endpoint",
    "pulse_compute_seconds",
    "rate_limit_429_total",
    "record_http_request",
    "submissions_total",
]


# ---------------------------------------------------------------------------
# Metric declarations — keep label sets small + stable.
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "rrxiv_http_requests_total",
    "Total HTTP requests served, labelled by route pattern + auth kind.",
    labelnames=("method", "path_pattern", "status", "auth_kind"),
)

annotations_posted_total = Counter(
    "rrxiv_annotations_posted_total",
    "Annotations successfully persisted, by type and identity kind.",
    labelnames=("annotation_type", "auth_kind", "target_kind"),
)

submissions_total = Counter(
    "rrxiv_submissions_total",
    "Paper submissions (including revisions). dry_run is excluded.",
    labelnames=("kind",),
)

rate_limit_429_total = Counter(
    "rrxiv_rate_limit_429_total",
    "Per-identity 429 responses fired by the sliding-window limiter.",
    labelnames=("auth_kind",),
)

pulse_compute_seconds = Histogram(
    "rrxiv_pulse_compute_seconds",
    "Wall-clock seconds spent inside compute_pulse(). High p99 here "
    "signals a corpus-size cliff worth checking.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def record_http_request(
    *, method: str, path_pattern: str, status: int, auth_kind: str
) -> None:
    """Counter bump from the access-log middleware. Keeps router code
    free of metrics imports."""
    try:
        http_requests_total.labels(
            method=method,
            path_pattern=path_pattern or "(unmatched)",
            status=str(status),
            auth_kind=auth_kind,
        ).inc()
    except Exception:
        # Never let telemetry block a real request.
        pass


async def metrics_endpoint(request: Any) -> Response:
    """FastAPI handler for ``GET /metrics``. Returns the Prometheus
    exposition payload with the canonical content-type. ``request``
    is unused but the FastAPI router passes it positionally; we
    accept it as ``Any`` to keep the signature flexible."""
    del request
    body = (
        generate_latest()
        if _HAVE_PROM
        else b"# prometheus-client not installed\n"
    )
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
