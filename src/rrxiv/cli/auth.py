"""``rrxiv auth`` — manage ORCID-bound signing keys (RRP-0024).

Subcommands:

- ``rrxiv auth bind-key``    — generate an Ed25519 keypair, prove
  possession, and bind its public half to your ORCID iD.
- ``rrxiv auth keys list``   — list your bound signing keys.
- ``rrxiv auth keys revoke`` — soft-revoke a bound key by id.

Requires a stored ORCID bearer (run ``rrxiv login orcid`` first) and the
``cryptography`` library (the ``[agent]`` extra). Once a key is bound,
``rrxiv submit --identity orcid`` and ``rrxiv annotation post --identity
orcid`` automatically RFC-9421-sign each write with it, so a stolen bearer
alone can no longer forge a write.
"""

from __future__ import annotations

import base64
import datetime
import secrets
import socket
from typing import Annotated

import typer

from rrxiv.cli.credentials import (
    StoredBearer,
    StoredOrcidKey,
    delete_orcid_key,
    load_bearer,
    load_orcid_key,
    store_orcid_key,
)
from rrxiv.cli.login import DEFAULT_SERVER_ENV, _now, _resolved_server

auth_app = typer.Typer(
    no_args_is_help=True,
    help="Bind and manage ORCID signing keys (RRP-0024).",
)
keys_app = typer.Typer(
    no_args_is_help=True,
    help="List and revoke ORCID-bound signing keys.",
)
auth_app.add_typer(keys_app, name="keys")

# Re-export so the indirection is visible to readers grepping this module.
__all__ = ["auth_app"]


def _ok(msg: str) -> None:
    typer.secho(f"  ✓ {msg}", fg=typer.colors.GREEN)


def _err(msg: str) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)


def _require_orcid_login(api_base: str) -> tuple[StoredBearer, str]:
    """Load a non-expired ORCID bearer + its orcid_id, or exit(2)."""
    bearer = load_bearer(api_base, "orcid")
    if bearer is None:
        _err(f"no ORCID login for {api_base}. Run: rrxiv login orcid")
        raise typer.Exit(2)
    if bearer.is_expired():
        _err("your ORCID bearer has expired. Run: rrxiv login orcid")
        raise typer.Exit(2)
    if bearer.identity is None:
        _err("stored ORCID bearer has no orcid_id; re-run rrxiv login orcid")
        raise typer.Exit(2)
    return bearer, bearer.identity


@auth_app.command("bind-key")
def bind_key(
    label: Annotated[
        str,
        typer.Option(
            "--label", help="Human label for the key (defaults to this hostname)."
        ),
    ] = "",
    server: Annotated[
        str | None,
        typer.Option(
            "--server",
            help=f"rrxiv API base URL. Defaults to ${DEFAULT_SERVER_ENV}.",
        ),
    ] = None,
) -> None:
    """Generate an Ed25519 keypair and bind it to your ORCID iD."""
    api_base = _resolved_server(server)
    bearer, orcid_id = _require_orcid_login(api_base)
    label = label or socket.gethostname()

    if label.startswith("@"):
        _err("label MUST NOT start with @ (reserved for agent handles)")
        raise typer.Exit(2)

    try:
        from rrxiv.auth import (
            OrcidKeyBindRequest,
            bind_orcid_key,
            build_key_binding_payload,
            sign_enrollment_payload,
        )
        from rrxiv.client.signatures import AgentSigningKey
    except ImportError as e:
        _err(
            f"binding a key needs the [agent] extra (cryptography): {e}. "
            "Install with: pip install 'rrxiv[agent]'"
        )
        raise typer.Exit(2) from e

    signing = AgentSigningKey.generate(handle="orcid-binding")  # handle unused here
    _ok("generated Ed25519 keypair")

    pub_b64 = base64.standard_b64encode(signing.public_key_bytes()).decode("ascii")
    payload = build_key_binding_payload(
        orcid_id=orcid_id,
        public_key_b64=pub_b64,
        nonce=secrets.token_hex(16),
        issued_at=_now(),
    )
    sig_b64 = sign_enrollment_payload(
        payload=payload, private_key_bytes=signing.private_key_bytes()
    )
    _ok("signed proof-of-possession payload")

    request = OrcidKeyBindRequest(
        public_key_b64=pub_b64,
        label=label,
        payload_b64=base64.standard_b64encode(payload).decode("ascii"),
        signature_b64=sig_b64,
    )
    record = bind_orcid_key(
        api_base=api_base, bearer=bearer.as_bearer(), request=request
    )
    _ok(f"POST /auth/orcid/keys → {record.key_id}")

    store_orcid_key(
        StoredOrcidKey(
            api_base=api_base,
            orcid_id=orcid_id,
            key_id=record.key_id,
            private_key_b64=base64.standard_b64encode(
                signing.private_key_bytes()
            ).decode("ascii"),
            label=label,
        )
    )
    _ok(f"persisted private key in keyring under ({api_base}, {orcid_id})")
    typer.echo()
    typer.secho(
        "Done. `rrxiv submit --identity orcid` and `rrxiv annotation post "
        "--identity orcid` will now sign writes with this key.",
        fg=typer.colors.GREEN,
    )


@keys_app.command("list")
def keys_list(
    server: Annotated[str | None, typer.Option("--server")] = None,
    include_revoked: Annotated[
        bool, typer.Option("--include-revoked", help="Also show revoked keys.")
    ] = False,
) -> None:
    """List your ORCID-bound signing keys (active set by default)."""
    api_base = _resolved_server(server)
    bearer, orcid_id = _require_orcid_login(api_base)

    from rrxiv.auth import list_orcid_keys

    records = list_orcid_keys(
        api_base=api_base,
        bearer=bearer.as_bearer(),
        include_revoked=include_revoked,
    )
    if not records:
        typer.echo("No bound keys. Run `rrxiv auth bind-key` to add one.")
        return

    local = load_orcid_key(api_base, orcid_id)
    local_key_id = local.key_id if local else None
    for r in records:
        created = (
            datetime.datetime.fromtimestamp(r.created_at_unix, tz=datetime.UTC)
            .date()
            .isoformat()
        )
        marker = "  (this machine)" if r.key_id == local_key_id else ""
        revoked = "  [REVOKED]" if r.revoked_at_unix else ""
        typer.echo(f"{r.key_id}  {(r.label or '—'):<20} {created}{marker}{revoked}")


@keys_app.command("revoke")
def keys_revoke(
    key_id: Annotated[
        str, typer.Argument(help="The key id to revoke, e.g. key:7d8e9f...")
    ],
    server: Annotated[str | None, typer.Option("--server")] = None,
) -> None:
    """Soft-revoke a bound key. Historical signatures stay verifiable."""
    api_base = _resolved_server(server)
    bearer, orcid_id = _require_orcid_login(api_base)

    from rrxiv.auth import revoke_orcid_key

    revoke_orcid_key(api_base=api_base, bearer=bearer.as_bearer(), key_id=key_id)
    _ok(f"revoked {key_id}")

    # If this machine held that key, drop the local private half so we stop
    # signing with a now-rejected key.
    local = load_orcid_key(api_base, orcid_id)
    if local and local.key_id == key_id:
        delete_orcid_key(api_base, orcid_id)
        _ok("removed the local private key")
