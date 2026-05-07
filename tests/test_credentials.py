"""Tests for rrxiv.cli.credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from rrxiv.cli.credentials import (
    StoredAgentKey,
    StoredBearer,
    delete_agent_key,
    delete_bearer,
    load_agent_key,
    load_bearer,
    store_agent_key,
    store_bearer,
    stored_identities_for_server,
    stored_servers,
)


@pytest.fixture()
def tmp_cred_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force file backend with a tmpdir-rooted credentials dir."""
    monkeypatch.setenv("RRXIV_CRED_BACKEND", "file")
    monkeypatch.setenv("RRXIV_CRED_DIR", str(tmp_path / "rrxiv"))
    return tmp_path / "rrxiv"


def test_store_and_load_bearer(tmp_cred_dir: Path) -> None:
    record = StoredBearer(
        api_base="https://rrxiv.com/api/v0",
        identity_type="orcid",
        identity="0000-0001-2345-6789",
        token="tok-abc",
        issued_at_unix=1700000000,
        expires_at_unix=1700003600,
    )
    store_bearer(record)
    loaded = load_bearer("https://rrxiv.com/api/v0", "orcid")
    assert loaded == record


def test_load_missing_returns_none(tmp_cred_dir: Path) -> None:
    assert load_bearer("https://nope.example/api/v0", "orcid") is None


def test_delete_bearer(tmp_cred_dir: Path) -> None:
    store_bearer(
        StoredBearer(
            api_base="https://rrxiv.com/api/v0",
            identity_type="orcid",
            identity="x",
            token="t",
            issued_at_unix=1,
            expires_at_unix=2,
        )
    )
    delete_bearer("https://rrxiv.com/api/v0", "orcid")
    assert load_bearer("https://rrxiv.com/api/v0", "orcid") is None


def test_multiple_servers_isolated(tmp_cred_dir: Path) -> None:
    store_bearer(
        StoredBearer(
            api_base="https://a.example/api/v0",
            identity_type="orcid",
            identity="a",
            token="ta",
            issued_at_unix=1,
            expires_at_unix=2,
        )
    )
    store_bearer(
        StoredBearer(
            api_base="https://b.example/api/v0",
            identity_type="orcid",
            identity="b",
            token="tb",
            issued_at_unix=1,
            expires_at_unix=2,
        )
    )
    assert {s for s in stored_servers()} == {
        "https://a.example/api/v0",
        "https://b.example/api/v0",
    }
    assert load_bearer("https://a.example/api/v0", "orcid").identity == "a"
    assert load_bearer("https://b.example/api/v0", "orcid").identity == "b"


def test_agent_key_round_trip(tmp_cred_dir: Path) -> None:
    record = StoredAgentKey(
        api_base="https://rrxiv.com/api/v0",
        handle="@my-bot",
        private_key_b64="QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQT0=",
    )
    store_agent_key(record)
    loaded = load_agent_key("https://rrxiv.com/api/v0", "@my-bot")
    assert loaded == record


def test_delete_agent_key(tmp_cred_dir: Path) -> None:
    record = StoredAgentKey(
        api_base="https://rrxiv.com/api/v0",
        handle="@to-delete",
        private_key_b64="aGVsbG8=",
    )
    store_agent_key(record)
    delete_agent_key("https://rrxiv.com/api/v0", "@to-delete")
    assert load_agent_key("https://rrxiv.com/api/v0", "@to-delete") is None


def test_stored_identities_for_server_lists_agents_and_bearers(
    tmp_cred_dir: Path,
) -> None:
    api_base = "https://rrxiv.com/api/v0"
    store_bearer(
        StoredBearer(
            api_base=api_base,
            identity_type="agent",
            identity="@bot1",
            token="t1",
            issued_at_unix=1,
            expires_at_unix=2,
        )
    )
    store_agent_key(
        StoredAgentKey(api_base=api_base, handle="@bot1", private_key_b64="x")
    )
    info = stored_identities_for_server(api_base)
    assert "agent" in info
    assert "agent_keys" in info
    assert "@bot1" in info["agent_keys"]


def test_credentials_file_is_chmod_0600(tmp_cred_dir: Path) -> None:
    store_bearer(
        StoredBearer(
            api_base="https://x.example/api/v0",
            identity_type="orcid",
            identity="x",
            token="t",
            issued_at_unix=1,
            expires_at_unix=2,
        )
    )
    cred_file = tmp_cred_dir / "credentials.json"
    assert cred_file.is_file()
    mode = cred_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_corrupted_file_treated_as_empty(tmp_cred_dir: Path) -> None:
    cred_file = tmp_cred_dir / "credentials.json"
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("not valid json {{{")
    # Loading should not blow up; just return None.
    assert load_bearer("any", "orcid") is None
