"""Tests for the 429 retry policy in the rrvix HTTP client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from rrvix.client import (
    NO_RETRY_POLICY,
    NotFoundError,
    RateLimitedError,
    RetryPolicy,
    RrvixClient,
    ServerError,
)
from rrvix.client.retry import (
    RetryBudget,
    compute_wait,
    is_retryable_status,
    parse_retry_after,
)


def _client_with_handler(
    handler: Any, retry_policy: RetryPolicy | None = None
) -> RrvixClient:
    transport = httpx.MockTransport(handler)
    return RrvixClient(
        "https://rrvix.org/api/v0",
        transport=transport,
        retry_policy=retry_policy,
    )


# ---- Pure helpers ----


class TestComputeWait:
    def test_uses_retry_after_when_present(self) -> None:
        policy = RetryPolicy(jitter=0.0)
        assert compute_wait(1, policy, retry_after_seconds=2.5) == 2.5

    def test_caps_retry_after_at_max(self) -> None:
        policy = RetryPolicy(backoff_max_seconds=10.0, jitter=0.0)
        assert compute_wait(1, policy, retry_after_seconds=999.0) == 10.0

    def test_exponential_when_no_retry_after(self) -> None:
        policy = RetryPolicy(backoff_initial_seconds=1.0, jitter=0.0)
        assert compute_wait(1, policy) == 1.0
        assert compute_wait(2, policy) == 2.0
        assert compute_wait(3, policy) == 4.0

    def test_jitter_within_band(self) -> None:
        policy = RetryPolicy(backoff_initial_seconds=1.0, jitter=0.25)
        # Run a few times; should fall within [0.75, 1.25]
        for _ in range(10):
            wait = compute_wait(1, policy)
            assert 0.75 <= wait <= 1.25


class TestRetryable:
    def test_429_always(self) -> None:
        assert is_retryable_status(429, RetryPolicy()) is True

    def test_5xx_only_when_opted_in(self) -> None:
        assert is_retryable_status(503, RetryPolicy(retry_on_5xx=False)) is False
        assert is_retryable_status(503, RetryPolicy(retry_on_5xx=True)) is True

    def test_4xx_other_than_429_no_retry(self) -> None:
        for code in (400, 401, 403, 404, 422):
            assert is_retryable_status(code, RetryPolicy(retry_on_5xx=True)) is False


class TestParseRetryAfter:
    def test_seconds_int(self) -> None:
        assert parse_retry_after("60") == 60.0

    def test_negative_clamped_to_zero(self) -> None:
        assert parse_retry_after("-5") == 0.0

    def test_garbage_returns_none(self) -> None:
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
        assert parse_retry_after("") is None
        assert parse_retry_after(None) is None


class TestRetryBudget:
    def test_under_max_retries(self) -> None:
        b = RetryBudget(RetryPolicy(max_retries=3, total_timeout_seconds=100))
        assert b.can_retry(1.0) is True
        b.spend(1.0)
        assert b.can_retry(1.0) is True
        b.spend(1.0)
        assert b.can_retry(1.0) is True
        b.spend(1.0)
        assert b.can_retry(1.0) is False  # at max

    def test_total_timeout_caps(self) -> None:
        b = RetryBudget(RetryPolicy(max_retries=10, total_timeout_seconds=5.0))
        b.spend(2.0)
        assert b.can_retry(2.0) is True
        b.spend(2.0)
        assert b.can_retry(2.0) is False  # 4 + 2 = 6 > 5


# ---- Integration with RrvixClient ----


class TestClientRetry:
    def test_succeeds_after_one_429(self) -> None:
        calls: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            if calls[0] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={
                    "rrvix_version": "0.1.0",
                    "id": "p1",
                    "version": "v1",
                    "title": "T",
                    "authors": [{"name": "A"}],
                    "abstract": "x",
                    "submitted_at": "2026-05-04T00:00:00Z",
                    "license": "CC-BY-4.0",
                    "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
                },
            )

        with _client_with_handler(handler) as client:
            paper = client.get_paper("p1")
        assert paper.id == "p1"
        assert calls[0] == 2

    def test_gives_up_after_max_retries(self) -> None:
        calls: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(429, headers={"Retry-After": "0"})

        policy = RetryPolicy(
            max_retries=2, backoff_initial_seconds=0, backoff_max_seconds=0, jitter=0.0
        )
        with _client_with_handler(handler, retry_policy=policy) as client:
            with pytest.raises(RateLimitedError):
                client.get_paper("p1")
        # initial + 2 retries = 3 total
        assert calls[0] == 3

    def test_no_retry_policy_disables_loop(self) -> None:
        calls: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(429)

        with _client_with_handler(handler, retry_policy=NO_RETRY_POLICY) as client:
            with pytest.raises(RateLimitedError):
                client.get_paper("p1")
        assert calls[0] == 1

    def test_5xx_no_retry_by_default(self) -> None:
        calls: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(503)

        with _client_with_handler(handler) as client:
            with pytest.raises(ServerError):
                client.get_paper("p1")
        assert calls[0] == 1  # no retry on 5xx by default

    def test_5xx_retries_when_opted_in(self) -> None:
        calls: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            if calls[0] < 2:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "id": "p1:c1",
                    "statement": "X.",
                    "claim_type": "theoretical",
                    "evidence_type": "argument",
                },
            )

        policy = RetryPolicy(
            retry_on_5xx=True,
            max_retries=3,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            jitter=0.0,
        )
        with _client_with_handler(handler, retry_policy=policy) as client:
            claim = client.get_claim("p1:c1")
        assert claim.id == "p1:c1"
        assert calls[0] == 2

    def test_4xx_other_than_429_does_not_retry(self) -> None:
        calls: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(
                404,
                content=json.dumps({"title": "Not found", "detail": "."}).encode(),
                headers={"content-type": "application/problem+json"},
            )

        with _client_with_handler(handler) as client:
            with pytest.raises(NotFoundError):
                client.get_paper("p1")
        assert calls[0] == 1
