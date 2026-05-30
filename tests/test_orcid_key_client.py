"""Client-side tests for ORCID key binding (RRP-0024).

The server endpoints + signature middleware are covered by
``test_orcid_key_binding.py``. These tests cover the *client* additions:

- the ``rrxiv.auth.orcid`` bind/list/revoke helpers (against the in-memory
  reference server via an httpx transport),
- ``StoredOrcidKey`` credential storage,
- the ``_resolve_identity`` wiring that makes ``submit`` / ``annotation
  post`` sign ORCID writes with a bound key,
- the ``rrxiv auth`` CLI guard rails.
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest

from rrxiv.auth import exchange_orcid_code
from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

API_BASE = "http://test/api/v0"
DEV_ORCID = "0000-0001-0000-DEV1"  # ServerSettings.orcid_dev_id default


def _client() -> tuple[Any, httpx.BaseTransport]:
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    return app, TestClient(app)._transport


def _orcid_bearer(transport: httpx.BaseTransport) -> Any:
    with httpx.Client(transport=transport, base_url=API_BASE) as c:
        resp = c.get(
            "/auth/orcid/start",
            params={"redirect_uri": "http://x/cb", "state": "s"},
            follow_redirects=False,
        )
    code = resp.headers["location"].split("code=", 1)[1].split("&")[0]
    return exchange_orcid_code(
        api_base=API_BASE, code=code, state="s", expected_state="s", transport=transport
    )


def _make_bind_request(orcid_id: str) -> tuple[Any, Any]:
    """Return ``(OrcidKeyBindRequest, AgentSigningKey)`` for a fresh key."""
    from rrxiv.auth import OrcidKeyBindRequest, build_key_binding_payload, sign_enrollment_payload
    from rrxiv.client import AgentSigningKey

    sk = AgentSigningKey.generate(handle="unused")
    pub_b64 = base64.standard_b64encode(sk.public_key_bytes()).decode("ascii")
    payload = build_key_binding_payload(
        orcid_id=orcid_id, public_key_b64=pub_b64, nonce=secrets.token_hex(16)
    )
    sig_b64 = sign_enrollment_payload(payload=payload, private_key_bytes=sk.private_key_bytes())
    request = OrcidKeyBindRequest(
        public_key_b64=pub_b64,
        label="laptop",
        payload_b64=base64.standard_b64encode(payload).decode("ascii"),
        signature_b64=sig_b64,
    )
    return request, sk


# ----------------------------- client helpers (networked) -----------------------------


def test_client_bind_list_revoke_roundtrip() -> None:
    from rrxiv.auth import bind_orcid_key, list_orcid_keys, revoke_orcid_key

    _app, transport = _client()
    bearer = _orcid_bearer(transport)
    request, _sk = _make_bind_request(bearer.identity)

    rec = bind_orcid_key(api_base=API_BASE, bearer=bearer, request=request, transport=transport)
    assert rec.key_id.startswith("key:")
    assert rec.label == "laptop"
    assert rec.orcid_id == bearer.identity
    assert rec.revoked_at_unix is None

    keys = list_orcid_keys(api_base=API_BASE, bearer=bearer, transport=transport)
    assert [k.key_id for k in keys] == [rec.key_id]

    revoke_orcid_key(api_base=API_BASE, bearer=bearer, key_id=rec.key_id, transport=transport)
    assert list_orcid_keys(api_base=API_BASE, bearer=bearer, transport=transport) == []

    with_revoked = list_orcid_keys(
        api_base=API_BASE, bearer=bearer, include_revoked=True, transport=transport
    )
    assert len(with_revoked) == 1
    assert with_revoked[0].revoked_at_unix is not None


def test_client_revoke_unknown_key_raises() -> None:
    from rrxiv.auth import revoke_orcid_key
    from rrxiv.client.errors import NotFoundError

    _app, transport = _client()
    bearer = _orcid_bearer(transport)
    with pytest.raises(NotFoundError):
        revoke_orcid_key(
            api_base=API_BASE, bearer=bearer, key_id="key:doesnotexist", transport=transport
        )


# ----------------------------- credential storage -----------------------------


@pytest.fixture()
def tmp_cred_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RRXIV_CRED_BACKEND", "file")
    monkeypatch.setenv("RRXIV_CRED_DIR", str(tmp_path / "rrxiv"))
    return tmp_path / "rrxiv"


def test_stored_orcid_key_roundtrip(tmp_cred_dir: Path) -> None:
    from rrxiv.cli.credentials import (
        StoredOrcidKey,
        delete_orcid_key,
        load_orcid_key,
        store_orcid_key,
    )

    rec = StoredOrcidKey(
        api_base=API_BASE,
        orcid_id=DEV_ORCID,
        key_id="key:abc123",
        private_key_b64=base64.standard_b64encode(b"\x00" * 32).decode("ascii"),
        label="laptop",
    )
    store_orcid_key(rec)
    assert load_orcid_key(API_BASE, DEV_ORCID) == rec
    assert rec.private_key_bytes() == b"\x00" * 32
    delete_orcid_key(API_BASE, DEV_ORCID)
    assert load_orcid_key(API_BASE, DEV_ORCID) is None


# ----------------------------- auto-signing wiring -----------------------------


def _store_orcid_identity(*, with_key: bool) -> Any:
    """Persist an ORCID bearer (+ optional bound key) and return the
    generating AgentSigningKey (or None)."""
    from rrxiv.cli.credentials import StoredBearer, StoredOrcidKey, store_bearer, store_orcid_key
    from rrxiv.client import AgentSigningKey

    store_bearer(
        StoredBearer(
            api_base=API_BASE,
            identity_type="orcid",
            identity=DEV_ORCID,
            token="tok",
            issued_at_unix=0,
            expires_at_unix=None,
        )
    )
    if not with_key:
        return None
    sk = AgentSigningKey.generate(handle="unused")
    store_orcid_key(
        StoredOrcidKey(
            api_base=API_BASE,
            orcid_id=DEV_ORCID,
            key_id="key:deadbeef",
            private_key_b64=base64.standard_b64encode(sk.private_key_bytes()).decode("ascii"),
            label="laptop",
        )
    )
    return sk


@pytest.mark.parametrize("module", ["submit", "annotation_post"])
def test_resolve_identity_signs_with_bound_orcid_key(
    tmp_cred_dir: Path, module: str
) -> None:
    import importlib

    sk = _store_orcid_identity(with_key=True)
    _resolve_identity = importlib.import_module(f"rrxiv.cli.{module}")._resolve_identity

    resolved = _resolve_identity(server=API_BASE, requested="orcid")
    assert resolved is not None
    kind, token, signing = resolved
    assert kind == "orcid"
    assert token == "tok"
    assert signing is not None
    assert signing.handle == "key:deadbeef"
    assert signing.private_key_bytes() == sk.private_key_bytes()


@pytest.mark.parametrize("module", ["submit", "annotation_post"])
def test_resolve_identity_orcid_without_bound_key_is_bearer_only(
    tmp_cred_dir: Path, module: str
) -> None:
    import importlib

    _store_orcid_identity(with_key=False)
    _resolve_identity = importlib.import_module(f"rrxiv.cli.{module}")._resolve_identity

    resolved = _resolve_identity(server=API_BASE, requested="orcid")
    assert resolved is not None
    kind, token, signing = resolved
    assert kind == "orcid"
    assert token == "tok"
    assert signing is None  # backward-compatible bearer-only write


# ----------------------------- CLI guard rails -----------------------------


def test_auth_keys_list_requires_orcid_login(tmp_cred_dir: Path) -> None:
    from typer.testing import CliRunner

    from rrxiv.cli.app import app

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--server", API_BASE])
    assert result.exit_code == 2


def test_auth_bind_key_requires_orcid_login(tmp_cred_dir: Path) -> None:
    from typer.testing import CliRunner

    from rrxiv.cli.app import app

    result = CliRunner().invoke(app, ["auth", "bind-key", "--server", API_BASE])
    assert result.exit_code == 2


@pytest.mark.parametrize("identity_type", [None, "orcid"])
def test_logout_forgets_bound_orcid_key(tmp_cred_dir: Path, identity_type: Any) -> None:
    from rrxiv.cli.credentials import load_orcid_key
    from rrxiv.cli.login import _logout_server

    _store_orcid_identity(with_key=True)
    assert load_orcid_key(API_BASE, DEV_ORCID) is not None
    _logout_server(API_BASE, identity_type=identity_type)
    assert load_orcid_key(API_BASE, DEV_ORCID) is None
