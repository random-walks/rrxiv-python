"""Smoke test for the Prometheus ``/metrics`` endpoint.

Confirms:
  - Endpoint mounts at the root, NOT under ``/api/v0`` (operational
    surface, not protocol).
  - Returns ``text/plain; version=0.0.4; charset=utf-8`` (the standard
    Prometheus exposition content-type).
  - Body includes the declared metric families even before any
    counter inc — Prometheus exposition includes the HELP + TYPE
    lines regardless.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rrxiv.server.app import build_app


def _client() -> TestClient:
    return TestClient(build_app())


def test_metrics_endpoint_exposes_prometheus_format() -> None:
    with _client() as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus standard content-type.
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # Declared metric families show up even with zero observations.
    assert "rrxiv_http_requests_total" in body
    assert "rrxiv_annotations_posted_total" in body
    assert "rrxiv_submissions_total" in body
    assert "rrxiv_rate_limit_429_total" in body
    assert "rrxiv_pulse_compute_seconds" in body


def test_metrics_not_under_api_prefix() -> None:
    """The /metrics endpoint is operational, not protocol. Third-party
    rrxiv clients should never see it on /api/v0."""
    with _client() as client:
        resp = client.get("/api/v0/metrics")
    # FastAPI returns 404 when no route matches the prefix.
    assert resp.status_code == 404


def test_http_requests_counter_increments_after_a_request() -> None:
    """Hit /api/v0/papers (cheap, public) and confirm the counter
    bumps in the next /metrics scrape.

    NOTE: /api/v0/version is in the middleware's NOISY_PATHS skip set
    (Fly's 30-second healthcheck hits it) so it does NOT increment
    rrxiv_http_requests_total. Use any other endpoint here.
    """
    with _client() as client:
        client.get("/api/v0/papers")
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "rrxiv_http_requests_total" in resp.text
    # At least one labelled entry with status=200 must exist.
    assert 'status="200"' in resp.text
