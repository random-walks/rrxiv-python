"""Credential storage for the ``rrxiv login`` family per RRP-0006.

Backends, in order of preference:

1. **OS-native keyring** via the `keyring` library — macOS Keychain,
   Windows Credential Locker, Linux Secret Service. Default when
   available.
2. **0600-permissioned JSON file** at ``~/.config/rrxiv/credentials.json``
   — fallback for headless environments (CI, containers, SSH-only
   boxes) where no system keyring backend is configured.

Public surface:

- :class:`StoredBearer` — bearer + metadata.
- :class:`StoredAgentKey` — agent's private key + handle.
- :func:`store_bearer` / :func:`load_bearer` / :func:`delete_bearer`.
- :func:`store_agent_key` / :func:`load_agent_key` / :func:`delete_agent_key`.
- :func:`stored_servers`, :func:`stored_identities_for_server`.

All keys are scoped to ``(api_base, identity_type)`` so a developer
can be logged in to multiple servers and multiple identity types
simultaneously.
"""

from __future__ import annotations

import json
import os
import time
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rrxiv.client.auth import BearerToken, IdentityType

KEYRING_SERVICE = "rrxiv"


@dataclass(frozen=True, slots=True)
class StoredBearer:
    """A persisted bearer token with the metadata we'll surface in
    ``rrxiv login status``."""

    api_base: str
    identity_type: IdentityType
    identity: str | None
    token: str
    issued_at_unix: int
    expires_at_unix: int | None

    def as_bearer(self) -> BearerToken:
        return BearerToken(
            token=self.token,
            identity_type=self.identity_type,
            identity=self.identity,
        )

    def is_expired(self, *, now_unix: int | None = None) -> bool:
        if self.expires_at_unix is None:
            return False
        return self.expires_at_unix <= (now_unix or int(time.time()))


@dataclass(frozen=True, slots=True)
class StoredAgentKey:
    """A persisted agent private key + handle."""

    api_base: str
    handle: str
    private_key_b64: str

    def private_key_bytes(self) -> bytes:
        return b64decode(self.private_key_b64)


@dataclass(frozen=True, slots=True)
class StoredOrcidKey:
    """A persisted ORCID-bound signing key (RRP-0024).

    One entry per ``(api_base, orcid_id)`` — the key *this machine* bound.
    The server may hold several keys for the same ORCID (one per machine);
    we only ever hold the private half of our own. ``key_id`` is the
    server-minted ``key:<hex>`` used as the RFC-9421 ``keyid`` on writes.
    """

    api_base: str
    orcid_id: str
    key_id: str
    private_key_b64: str
    label: str = ""

    def private_key_bytes(self) -> bytes:
        return b64decode(self.private_key_b64)


# ----------------------------- backend selection -----------------------------


def _keyring_available() -> bool:
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        from keyring.backends.null import Keyring as NullKeyring
    except ImportError:
        return False
    backend = keyring.get_keyring()
    return not isinstance(backend, (FailKeyring, NullKeyring))


def _backend() -> Literal["keyring", "file"]:
    if os.environ.get("RRXIV_CRED_BACKEND") == "file":
        return "file"
    if os.environ.get("RRXIV_CRED_BACKEND") == "keyring":
        return "keyring"
    return "keyring" if _keyring_available() else "file"


# ----------------------------- bearer storage -----------------------------


def _bearer_username(api_base: str, identity_type: str) -> str:
    return f"{api_base}:{identity_type}"


def _agent_key_username(api_base: str, handle: str) -> str:
    return f"{api_base}:agent:{handle}:private-key"


def _orcid_key_username(api_base: str, orcid_id: str) -> str:
    return f"{api_base}:orcid:{orcid_id}:signing-key"


def store_bearer(record: StoredBearer) -> None:
    """Persist a bearer token. Overwrites any existing entry."""
    if _backend() == "keyring":
        import keyring

        keyring.set_password(
            KEYRING_SERVICE,
            _bearer_username(record.api_base, record.identity_type),
            json.dumps(_bearer_to_dict(record)),
        )
    else:
        _file_update(lambda d: _file_set_bearer(d, record))


def load_bearer(api_base: str, identity_type: IdentityType) -> StoredBearer | None:
    """Load a bearer for ``(api_base, identity_type)``, or None."""
    if _backend() == "keyring":
        import keyring

        raw = keyring.get_password(
            KEYRING_SERVICE, _bearer_username(api_base, identity_type)
        )
        if raw is None:
            return None
        return _bearer_from_dict(json.loads(raw))
    return _file_load_bearer(api_base, identity_type)


def delete_bearer(api_base: str, identity_type: IdentityType) -> None:
    """Delete a bearer entry. No error if absent."""
    if _backend() == "keyring":
        import keyring

        try:
            keyring.delete_password(
                KEYRING_SERVICE, _bearer_username(api_base, identity_type)
            )
        except keyring.errors.PasswordDeleteError:
            pass
    else:
        _file_update(lambda d: _file_delete_bearer(d, api_base, identity_type))


# ----------------------------- agent keys -----------------------------


def store_agent_key(record: StoredAgentKey) -> None:
    if _backend() == "keyring":
        import keyring

        keyring.set_password(
            KEYRING_SERVICE,
            _agent_key_username(record.api_base, record.handle),
            json.dumps(_agent_key_to_dict(record)),
        )
    else:
        _file_update(lambda d: _file_set_agent_key(d, record))


def load_agent_key(api_base: str, handle: str) -> StoredAgentKey | None:
    if _backend() == "keyring":
        import keyring

        raw = keyring.get_password(
            KEYRING_SERVICE, _agent_key_username(api_base, handle)
        )
        if raw is None:
            return None
        return _agent_key_from_dict(json.loads(raw))
    return _file_load_agent_key(api_base, handle)


def delete_agent_key(api_base: str, handle: str) -> None:
    if _backend() == "keyring":
        import keyring

        try:
            keyring.delete_password(
                KEYRING_SERVICE, _agent_key_username(api_base, handle)
            )
        except keyring.errors.PasswordDeleteError:
            pass
    else:
        _file_update(lambda d: _file_delete_agent_key(d, api_base, handle))


# ----------------------------- orcid signing keys (RRP-0024) -----------------------------


def store_orcid_key(record: StoredOrcidKey) -> None:
    if _backend() == "keyring":
        import keyring

        keyring.set_password(
            KEYRING_SERVICE,
            _orcid_key_username(record.api_base, record.orcid_id),
            json.dumps(_orcid_key_to_dict(record)),
        )
    else:
        _file_update(lambda d: _file_set_orcid_key(d, record))


def load_orcid_key(api_base: str, orcid_id: str) -> StoredOrcidKey | None:
    if _backend() == "keyring":
        import keyring

        raw = keyring.get_password(
            KEYRING_SERVICE, _orcid_key_username(api_base, orcid_id)
        )
        if raw is None:
            return None
        return _orcid_key_from_dict(json.loads(raw))
    return _file_load_orcid_key(api_base, orcid_id)


def delete_orcid_key(api_base: str, orcid_id: str) -> None:
    if _backend() == "keyring":
        import keyring

        try:
            keyring.delete_password(
                KEYRING_SERVICE, _orcid_key_username(api_base, orcid_id)
            )
        except keyring.errors.PasswordDeleteError:
            pass
    else:
        _file_update(lambda d: _file_delete_orcid_key(d, api_base, orcid_id))


# ----------------------------- introspection -----------------------------


def stored_servers() -> list[str]:
    """List API bases for which any credential is stored.

    File backend: read directly. Keyring backend: not enumerable, so
    we maintain a sidecar index file at the file-fallback path so
    ``rrxiv login status`` can list across servers.
    """
    if _backend() == "file":
        d = _file_load()
    else:
        d = _file_load_index()
    return sorted(d.get("credentials", {}).keys())


def stored_identities_for_server(api_base: str) -> dict[str, dict[str, Any]]:
    """Returns ``{identity_type: {…metadata…}}`` for the given server.

    For agents, the dict contains a ``handles`` mapping
    ``{handle: {has_private_key: bool}}``.
    """
    if _backend() == "file":
        d = _file_load()
    else:
        d = _file_load_index()
    out: dict[str, dict[str, Any]] = d.get("credentials", {}).get(api_base, {})
    return out


# ----------------------------- file backend -----------------------------


def _credentials_path() -> Path:
    base = Path(os.environ.get("RRXIV_CRED_DIR") or os.path.expanduser("~/.config/rrxiv"))
    return base / "credentials.json"


def _file_load() -> dict[str, Any]:
    path = _credentials_path()
    if not path.is_file():
        return {"version": 1, "credentials": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "credentials": {}}


def _file_save(data: dict[str, Any]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _file_update(mutator: Any) -> None:
    """Read-modify-write the file. Note: best-effort locking only."""
    data = _file_load()
    mutator(data)
    _file_save(data)


def _file_load_index() -> dict[str, Any]:
    """When using keyring, we still keep an index file so
    `stored_servers` is enumerable. Tracks just the keys, never the
    secrets."""
    return _file_load()


def _file_set_bearer(d: dict[str, Any], record: StoredBearer) -> None:
    d.setdefault("credentials", {}).setdefault(record.api_base, {})[
        record.identity_type
    ] = _bearer_to_dict(record)


def _file_load_bearer(
    api_base: str, identity_type: IdentityType
) -> StoredBearer | None:
    d = _file_load()
    raw = d.get("credentials", {}).get(api_base, {}).get(identity_type)
    if raw is None:
        return None
    return _bearer_from_dict(raw)


def _file_delete_bearer(
    d: dict[str, Any], api_base: str, identity_type: IdentityType
) -> None:
    server = d.get("credentials", {}).get(api_base, {})
    server.pop(identity_type, None)
    if not server:
        d.get("credentials", {}).pop(api_base, None)


def _file_set_agent_key(d: dict[str, Any], record: StoredAgentKey) -> None:
    server = d.setdefault("credentials", {}).setdefault(record.api_base, {})
    handles = server.setdefault("agent_keys", {})
    handles[record.handle] = _agent_key_to_dict(record)


def _file_load_agent_key(api_base: str, handle: str) -> StoredAgentKey | None:
    d = _file_load()
    raw = (
        d.get("credentials", {})
        .get(api_base, {})
        .get("agent_keys", {})
        .get(handle)
    )
    if raw is None:
        return None
    return _agent_key_from_dict(raw)


def _file_delete_agent_key(
    d: dict[str, Any], api_base: str, handle: str
) -> None:
    server = d.get("credentials", {}).get(api_base, {})
    handles = server.get("agent_keys", {})
    handles.pop(handle, None)
    if not handles:
        server.pop("agent_keys", None)
    if not server:
        d.get("credentials", {}).pop(api_base, None)


def _file_set_orcid_key(d: dict[str, Any], record: StoredOrcidKey) -> None:
    server = d.setdefault("credentials", {}).setdefault(record.api_base, {})
    keys = server.setdefault("orcid_keys", {})
    keys[record.orcid_id] = _orcid_key_to_dict(record)


def _file_load_orcid_key(api_base: str, orcid_id: str) -> StoredOrcidKey | None:
    d = _file_load()
    raw = (
        d.get("credentials", {})
        .get(api_base, {})
        .get("orcid_keys", {})
        .get(orcid_id)
    )
    if raw is None:
        return None
    return _orcid_key_from_dict(raw)


def _file_delete_orcid_key(
    d: dict[str, Any], api_base: str, orcid_id: str
) -> None:
    server = d.get("credentials", {}).get(api_base, {})
    keys = server.get("orcid_keys", {})
    keys.pop(orcid_id, None)
    if not keys:
        server.pop("orcid_keys", None)
    if not server:
        d.get("credentials", {}).pop(api_base, None)


# ----------------------------- (de)ser helpers -----------------------------


def _bearer_to_dict(record: StoredBearer) -> dict[str, Any]:
    return {
        "api_base": record.api_base,
        "identity_type": record.identity_type,
        "identity": record.identity,
        "token": record.token,
        "issued_at_unix": record.issued_at_unix,
        "expires_at_unix": record.expires_at_unix,
    }


def _bearer_from_dict(d: dict[str, Any]) -> StoredBearer:
    return StoredBearer(
        api_base=d["api_base"],
        identity_type=d["identity_type"],
        identity=d.get("identity"),
        token=d["token"],
        issued_at_unix=int(d["issued_at_unix"]),
        expires_at_unix=(
            int(d["expires_at_unix"]) if d.get("expires_at_unix") else None
        ),
    )


def _agent_key_to_dict(record: StoredAgentKey) -> dict[str, Any]:
    return {
        "api_base": record.api_base,
        "handle": record.handle,
        "private_key_b64": record.private_key_b64,
    }


def _agent_key_from_dict(d: dict[str, Any]) -> StoredAgentKey:
    return StoredAgentKey(
        api_base=d["api_base"],
        handle=d["handle"],
        private_key_b64=d["private_key_b64"],
    )


def _orcid_key_to_dict(record: StoredOrcidKey) -> dict[str, Any]:
    return {
        "api_base": record.api_base,
        "orcid_id": record.orcid_id,
        "key_id": record.key_id,
        "private_key_b64": record.private_key_b64,
        "label": record.label,
    }


def _orcid_key_from_dict(d: dict[str, Any]) -> StoredOrcidKey:
    return StoredOrcidKey(
        api_base=d["api_base"],
        orcid_id=d["orcid_id"],
        key_id=d["key_id"],
        private_key_b64=d["private_key_b64"],
        label=d.get("label", ""),
    )


__all__ = [
    "StoredAgentKey",
    "StoredBearer",
    "StoredOrcidKey",
    "delete_agent_key",
    "delete_bearer",
    "delete_orcid_key",
    "load_agent_key",
    "load_bearer",
    "load_orcid_key",
    "store_agent_key",
    "store_bearer",
    "store_orcid_key",
    "stored_identities_for_server",
    "stored_servers",
]
