"""Version single-sourcing: rrxiv.__version__ must match pyproject.toml.

Guards against the drift that shipped 0.2.x wheels whose runtime
reported 0.1.0 (wrong GET /version, wrong Sentry release tag).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import rrxiv


def test_dunder_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert rrxiv.__version__ == declared


def test_version_endpoint_reports_package_version() -> None:
    from fastapi.testclient import TestClient

    from rrxiv.server import ServerSettings, build_app

    client = TestClient(build_app(settings=ServerSettings(dev_mode=True)))
    body = client.get("/api/v0/version").json()
    assert body["server"] == f"rrxiv-python-server/{rrxiv.__version__}"
