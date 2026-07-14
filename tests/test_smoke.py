"""Smoke tests for rrxiv package."""

from rrxiv import __version__


def test_version() -> None:
    """Version is single-sourced from package metadata — the exact
    pyproject lockstep is asserted in tests/test_version.py."""
    assert __version__
    assert __version__ != "0.0.0.dev0"
