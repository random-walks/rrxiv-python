"""Shared pytest fixtures for the rrvix-python test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_client_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitute the HTTP client's sleep so retry tests run instantly.

    Applies to every test in the suite. The retry loop calls
    ``rrvix.client.client.sleep_for`` (a thin wrapper); mocking it
    keeps tests deterministic and fast even when the default retry
    policy backs off for several seconds.
    """
    monkeypatch.setattr("rrvix.client.client.sleep_for", lambda _s: None)
