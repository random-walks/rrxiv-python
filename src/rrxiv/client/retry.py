"""Retry policy for the rrxiv HTTP client.

The protocol's locked rate-limit floors mean any non-trivial agent or
batch script will hit 429 occasionally. Rather than make every caller
implement the retry loop, the client offers an opt-in policy that:

- Honours ``Retry-After`` if the server provides it.
- Falls back to exponential backoff with jitter otherwise.
- Caps total retries and total wall-clock wait so a client never hangs.
- Optionally retries 5xx (server errors) — off by default since most 5xx
  responses indicate a real bug, not a transient overload.

The default :data:`DEFAULT_RETRY_POLICY` is conservative: 3 retries,
30-second total cap, no 5xx retry. Callers who want more aggressive
behaviour pass their own :class:`RetryPolicy` to ``RrxivClient``.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry configuration for an :class:`rrxiv.client.RrxivClient`.

    Attributes:
        max_retries: Maximum number of retry attempts. ``0`` disables
            retries entirely.
        backoff_initial_seconds: Initial backoff if no ``Retry-After``
            header is present. Doubles on each retry.
        backoff_max_seconds: Cap on a single backoff interval.
        total_timeout_seconds: Cap on cumulative wait across all
            retries. The retry loop bails out once this is reached.
        retry_on_5xx: If True, also retry 5xx responses (with the same
            backoff). Default False — most 5xx responses indicate a
            real bug, not a transient.
        jitter: Multiplier on top of the deterministic backoff. With
            jitter=0.25, actual wait is uniformly distributed in
            [0.75 * b, 1.25 * b]. Reduces thundering-herd retries.
    """

    max_retries: int = 3
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    total_timeout_seconds: float = 30.0
    retry_on_5xx: bool = False
    jitter: float = 0.25


DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()
NO_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy(max_retries=0)

# Module-level RNG used when the caller doesn't pass one to compute_wait().
_DEFAULT_RNG: Final[random.Random] = random.Random()


def compute_wait(
    attempt: int,
    policy: RetryPolicy,
    *,
    retry_after_seconds: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Compute how long to wait before retry attempt ``attempt`` (1-indexed).

    If the server provided a ``Retry-After`` value, honour it (capped
    at ``backoff_max_seconds``). Otherwise compute exponential backoff
    with optional jitter.
    """
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return min(retry_after_seconds, policy.backoff_max_seconds)
    base = min(
        policy.backoff_initial_seconds * (2 ** (attempt - 1)),
        policy.backoff_max_seconds,
    )
    if policy.jitter > 0:
        rng_obj = rng if rng is not None else _DEFAULT_RNG
        r = rng_obj.uniform(1 - policy.jitter, 1 + policy.jitter)
        return max(0.0, float(base * r))
    return float(base)


def is_retryable_status(
    status_code: int, policy: RetryPolicy
) -> bool:
    if status_code == 429:
        return True
    if 500 <= status_code < 600 and policy.retry_on_5xx:
        return True
    return False


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value (seconds-int form only;
    HTTP-date form is rare in practice and not handled here)."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)


class RetryBudget:
    """Tracks cumulative retry wait against a policy's total cap.

    Used by the client's retry loop to bail out when the budget is
    exhausted.
    """

    def __init__(self, policy: RetryPolicy):
        self.policy = policy
        self._spent_seconds: float = 0.0
        self._attempts: int = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def spend(self, seconds: float) -> None:
        self._spent_seconds += seconds
        self._attempts += 1

    def can_retry(self, additional_wait: float) -> bool:
        if self._attempts >= self.policy.max_retries:
            return False
        return self._spent_seconds + additional_wait <= self.policy.total_timeout_seconds


def sleep_for(seconds: float) -> None:  # pragma: no cover - thin wrapper
    """Wrapper around :func:`time.sleep`; substituted in tests."""
    if seconds > 0:
        time.sleep(seconds)
