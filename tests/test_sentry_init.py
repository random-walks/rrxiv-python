"""Tests for the optional Sentry SDK initialisation in app.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reset_sentry_flag():
    """Reset the module-level ``_sentry_initialised`` flag between tests."""
    from rrxiv.server import app as app_module

    app_module._sentry_initialised = False
    yield
    app_module._sentry_initialised = False


def test_init_sentry_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    from rrxiv.server import app as app_module

    with patch("sentry_sdk.init") as mock_init:
        app_module._init_sentry()
    mock_init.assert_not_called()
    assert app_module._sentry_initialised is False


def test_init_sentry_calls_init_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.example.com/123")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "test-env")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.5")
    from rrxiv.server import app as app_module

    with patch("sentry_sdk.init") as mock_init:
        app_module._init_sentry()
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == "https://key@sentry.example.com/123"
    assert kwargs["environment"] == "test-env"
    assert kwargs["traces_sample_rate"] == 0.5
    assert kwargs["send_default_pii"] is False
    assert app_module._sentry_initialised is True


def test_init_sentry_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.example.com/123")
    from rrxiv.server import app as app_module

    with patch("sentry_sdk.init") as mock_init:
        app_module._init_sentry()
        app_module._init_sentry()
        app_module._init_sentry()
    mock_init.assert_called_once()


def test_init_sentry_handles_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.example.com/123")
    from rrxiv.server import app as app_module

    # Simulate sentry-sdk not installed by patching the import.
    with patch.dict("sys.modules", {"sentry_sdk": None}):
        # The function catches ImportError silently.
        app_module._init_sentry()
    assert app_module._sentry_initialised is False


def test_build_app_calls_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_app`` should trigger Sentry init exactly once."""
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.example.com/123")
    from rrxiv.server import app as app_module

    with patch("sentry_sdk.init") as mock_init:
        app_module.build_app()
    mock_init.assert_called_once()
