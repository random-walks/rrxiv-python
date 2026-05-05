"""Smoke tests for rrxiv package."""

from rrxiv import __version__


def test_version() -> None:
    assert __version__ == "0.1.0"
