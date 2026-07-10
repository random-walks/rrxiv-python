"""``rrxiv submit`` — submit a paper or revision to an rrxiv instance.

Wraps the multipart `POST /api/v0/submissions` endpoint (RRP-0008,
RRP-0016). Resolves the submitting identity from the keyring (per
RRP-0006) or honours an explicit ``--identity`` flag. Attaches an
Ed25519 signature for agent identities (RRP-0007). Supports dry-run
(RRP-0016) and revision-of (RRP-0017) modes.

Typical use::

    rrxiv submit paper/main.cir.json paper-v1.tar.gz
    rrxiv submit paper/main.cir.json paper-v2.tar.gz --revision-of <prior_paper_id>
    rrxiv submit paper/main.cir.json paper.tar.gz --dry-run

The default server is read from ``$RRXIV_SERVER`` (falls back to
``https://api.rrxiv.com/api/v0``). The default identity is whatever's
in the keychain — if multiple are stored, pass ``--identity orcid``,
``--identity agent``, or ``--server`` to disambiguate.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer

from rrxiv.cli.credentials import (
    load_agent_key,
    load_bearer,
    load_orcid_key,
)
from rrxiv.client.signatures import AgentSigningAuth, AgentSigningKey

DEFAULT_SERVER = os.environ.get("RRXIV_SERVER", "https://api.rrxiv.com/api/v0")


def submit(
    cir_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Path to the CIR JSON file (typically `build/main.cir.json`).",
        ),
    ],
    bundle_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Path to the source bundle (`.tar.gz`).",
        ),
    ],
    pdf_path: Annotated[
        Path | None,
        typer.Option(
            "--pdf",
            exists=True,
            readable=True,
            help=(
                "Path to the rendered PDF. If unset, the CLI auto-detects "
                "build/main.pdf next to the CIR/bundle. Pass --no-pdf to "
                "skip uploading a PDF (the submission still succeeds; the "
                "read-side PDF endpoint will 404)."
            ),
        ),
    ] = None,
    no_pdf: Annotated[
        bool,
        typer.Option(
            "--no-pdf",
            help="Submit without a PDF even if one is auto-detected.",
        ),
    ] = False,
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    identity: Annotated[
        str | None,
        typer.Option(
            "--identity",
            help=(
                "Which identity to use: 'orcid' or 'agent'. Required when "
                "multiple are stored for the same server."
            ),
        ),
    ] = None,
    revision_of: Annotated[
        str | None,
        typer.Option(
            "--revision-of",
            help=(
                "Paper ID of the prior version. The server will set "
                "previous_version + compute a revision_diff (RRP-0017)."
            ),
        ),
    ] = None,
    revision_summary: Annotated[
        str | None,
        typer.Option(
            "--revision-summary",
            help=(
                "Plaintext summary of changes for a revision. The server "
                "synthesises a revision_summary annotation (RRP-0017)."
            ),
        ),
    ] = None,
    revision_summary_file: Annotated[
        Path | None,
        typer.Option(
            "--revision-summary-file",
            exists=True,
            readable=True,
            help="Path to a file whose contents become the revision_summary.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Validate without persisting (RRP-0016 §Dry-run semantics). "
                "Server runs compile + parse + diff; nothing is stored."
            ),
        ),
    ] = False,
    skip_hash: Annotated[
        bool,
        typer.Option(
            "--skip-hash",
            help=(
                "Skip the client_compile_hash check. The server's "
                "advisory-not-required validation in v0.x means most "
                "submissions can omit it; defaults to sending the hash."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the response body as raw JSON."),
    ] = False,
) -> None:
    """Submit a paper or revision via ``POST /api/v0/submissions``."""

    # ---- Resolve identity ---------------------------------------------
    resolved_identity = _resolve_identity(server=server, requested=identity)
    if resolved_identity is None:
        typer.secho(
            (
                f"no stored identity for {server!r}. "
                "Run 'rrxiv login orcid' or 'rrxiv login agent' first."
            ),
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    identity_kind, bearer_token, agent_key = resolved_identity

    # ---- Read + hash bundle, prepare CIR ------------------------------
    bundle_bytes = bundle_path.read_bytes()
    cir_bytes = cir_path.read_bytes()
    try:
        json.loads(cir_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        typer.secho(
            f"CIR at {cir_path} is not valid UTF-8 JSON: {e}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2) from e

    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()

    # ---- Resolve PDF path --------------------------------------------
    # Auto-detect: build/main.pdf next to the bundle is the convention
    # produced by rrxiv-paper-template's scripts/build.sh.
    resolved_pdf: Path | None = None
    if not no_pdf:
        if pdf_path is not None:
            resolved_pdf = pdf_path
        else:
            candidate = bundle_path.parent / "main.pdf"
            if candidate.is_file():
                resolved_pdf = candidate
    pdf_bytes: bytes | None = (
        resolved_pdf.read_bytes() if resolved_pdf is not None else None
    )

    # ---- Resolve revision_summary -------------------------------------
    summary_text: str | None = None
    if revision_summary and revision_summary_file:
        typer.secho(
            "use either --revision-summary or --revision-summary-file, not both",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if revision_summary:
        summary_text = revision_summary
    elif revision_summary_file:
        summary_text = revision_summary_file.read_text(encoding="utf-8")

    # ---- Build multipart body -----------------------------------------
    files: dict[str, tuple[str, bytes, str]] = {
        "cir": (cir_path.name, cir_bytes, "application/json"),
        "bundle": (bundle_path.name, bundle_bytes, "application/gzip"),
    }
    if pdf_bytes is not None and resolved_pdf is not None:
        files["pdf"] = (resolved_pdf.name, pdf_bytes, "application/pdf")
    data: dict[str, str] = {}
    if revision_of:
        data["previous_version"] = revision_of
    if summary_text:
        data["revision_summary"] = summary_text
    if dry_run:
        data["dry_run"] = "true"
    if not skip_hash:
        data["client_compile_hash"] = bundle_hash

    # ---- Wire auth ----------------------------------------------------
    headers = {"Authorization": f"Bearer {bearer_token}"}
    sign_auth: AgentSigningAuth | None = None
    if identity_kind == "agent" and agent_key is not None:
        sign_auth = AgentSigningAuth(agent_key)

    # ---- POST ---------------------------------------------------------
    if not json_output:
        mode = "dry-run" if dry_run else ("revision" if revision_of else "submission")
        pdf_note = (
            f" + {resolved_pdf.name}" if resolved_pdf is not None else " (no PDF)"
        )
        typer.echo(
            f"==> {mode} to {server} as {identity_kind} "
            f"({bundle_path.name}{pdf_note}, sha256={bundle_hash[:12]}…)"
        )

    with httpx.Client(timeout=120.0) as client:
        # AgentSigningAuth is an httpx.Auth subclass; httpx accepts
        # None for auth at runtime but the stub's type union excludes
        # it. type: ignore both branches at the call site.
        resp = client.post(
            f"{server.rstrip('/')}/submissions",
            headers=headers,
            files=files,
            data=data,
            auth=cast("httpx.Auth | None", sign_auth),  # type: ignore[arg-type]
        )

    # ---- Render response ----------------------------------------------
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = {"raw": resp.text}

    if json_output:
        typer.echo(json.dumps(body, indent=2, sort_keys=True))
        if resp.status_code >= 400:
            raise typer.Exit(code=1)
        return

    if resp.status_code >= 400:
        typer.secho(
            f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True
        )
        typer.echo(json.dumps(body, indent=2), err=True)
        raise typer.Exit(code=1)

    _print_success(body, server=server, dry_run=dry_run)


def _resolve_identity(
    *, server: str, requested: str | None
) -> tuple[str, str, AgentSigningKey | None] | None:
    """Find a stored identity for ``server``.

    Returns ``(identity_kind, bearer_token, agent_key_or_none)`` or
    None if nothing's stored.
    """
    if requested in ("orcid", "agent", "anonymous"):
        kinds = [requested]
    elif requested is None:
        kinds = ["orcid", "agent"]  # anonymous can't submit
    else:
        typer.secho(
            f"unknown --identity value: {requested!r} (use orcid|agent|anonymous)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    for kind in kinds:
        bearer = load_bearer(server, kind)  # type: ignore[arg-type]
        if not bearer:
            continue
        if kind == "orcid":
            # RRP-0024: if this machine bound an ORCID signing key, sign
            # writes with it (keyid = key:...). Otherwise fall back to
            # bearer-only (still a valid v0 write).
            orcid_signing: AgentSigningKey | None = None
            if bearer.identity is not None:
                orcid_key = load_orcid_key(server, bearer.identity)
                if orcid_key is not None:
                    orcid_signing = AgentSigningKey.from_private_bytes(
                        handle=orcid_key.key_id,
                        private_key_bytes=orcid_key.private_key_bytes(),
                    )
            return kind, bearer.token, orcid_signing
        if kind != "agent":
            return kind, bearer.token, None  # anonymous: bearer only
        # Agent identity also needs the private key for signing.
        if bearer.identity is None:
            continue
        agent_key_record = load_agent_key(server, bearer.identity)
        if not agent_key_record:
            continue
        signing = AgentSigningKey.from_private_bytes(
            handle=agent_key_record.handle,
            private_key_bytes=agent_key_record.private_key_bytes(),
        )
        return kind, bearer.token, signing
    return None


def _web_view_url(server: str, slug: str) -> str:
    """Build the human-facing paper-page URL from the API server URL.

    The paper page lives on the WEB host, not the API host. On the
    canonical instance the API is served from ``api.rrxiv.com/api/v0``
    while the pages live on ``rrxiv.com`` — so
    ``https://api.rrxiv.com/papers/<slug>`` 404s. Strip the ``/api/v0``
    suffix and map the canonical API host to the web host. Non-canonical
    hosts (dev, self-hosted) serve pages from the same origin, so only
    the suffix is stripped.
    """
    base = server.replace("/api/v0", "").rstrip("/")
    # Canonical prod: api.rrxiv.com → rrxiv.com. Anchored on ``://`` so we
    # only rewrite the host, never a path segment.
    base = base.replace("://api.rrxiv.com", "://rrxiv.com", 1)
    return f"{base}/papers/{slug}"


def _print_success(body: dict[str, Any], *, server: str, dry_run: bool) -> None:
    """Pretty-print the response for human consumption."""
    paper_id = body.get("paper_id")
    slug = body.get("id_slug")
    version = body.get("version")
    prev = body.get("previous_version")
    diff = body.get("revision_diff")

    if dry_run:
        typer.secho("dry-run OK", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("submission OK", fg=typer.colors.GREEN, bold=True)

    if slug:
        typer.echo(f"  id_slug:         {slug}")
    if paper_id:
        typer.echo(f"  paper_id:        {paper_id}")
    if version:
        typer.echo(f"  version:         {version}")
    if prev:
        typer.echo(f"  previous:        {prev}")
    if body.get("retrieval_uri"):
        typer.echo(f"  retrieval_uri:   {body['retrieval_uri']}")
    if not dry_run and paper_id:
        # Quick-link to the human-friendly page on the canonical instance.
        canonical = (slug and _web_view_url(server, slug)) or None
        if canonical:
            typer.echo(f"  view:            {canonical}")
    if diff:
        added = len(diff.get("claims", {}).get("added", []))
        removed = len(diff.get("claims", {}).get("removed", []))
        modified = len(diff.get("claims", {}).get("modified", []))
        unchanged = diff.get("claims", {}).get("unchanged_count", 0)
        ab_changed = diff.get("abstract_changed", False)
        typer.echo(
            f"  diff vs v1:      claims +{added}/-{removed} (~{modified}, "
            f"={unchanged}){' · abstract changed' if ab_changed else ''}"
        )
