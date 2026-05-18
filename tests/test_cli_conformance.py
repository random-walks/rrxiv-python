"""Smoke-test the `rrxiv conformance` CLI subcommand (Phase 5)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from rrxiv.cli.app import app

pytest_plugins = ["rrxiv.testing.live_server"]

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("cryptography")


def test_conformance_cli_passes_against_reference_server(
    live_server,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end: drive `rrxiv conformance` against a real reference
    server. Should pass cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["conformance", live_server.url])
    assert result.exit_code == 0, result.output
    assert "Conformance suite passed" in result.output
