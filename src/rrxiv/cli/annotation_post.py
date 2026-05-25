"""``rrxiv annotation post`` / ``list`` and the top-level convenience
commands ``rrxiv retract`` / ``rrxiv replicate`` / ``rrxiv comment``.

Wraps the multipart-free JSON ``POST /api/v0/annotations`` endpoint plus
the read-side ``GET /api/v0/annotations`` for inspection. Mirrors the
agent/orcid auth resolution from ``submit.py`` — see RRP-0006 for the
identity model and RRP-0007 for the Ed25519 signing path.

Surfaced in Sprint 17 (2026-05-25) after Sprint 16 had to retract 44 v1
claims and the CLI had no annotation POST surface — we wrote a one-shot
Python script duplicating the signing logic. That smell is the
motivation for this module.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from rrxiv.cli.credentials import load_agent_key, load_bearer
from rrxiv.client.signatures import AgentSigningAuth, AgentSigningKey

DEFAULT_SERVER = os.environ.get("RRXIV_SERVER", "https://api.rrxiv.com/api/v0")


def _resolve_identity(
    *, server: str, requested: str | None
) -> tuple[str, str, AgentSigningKey | None] | None:
    """Find a stored identity for ``server`` (mirrors submit.py)."""
    if requested in ("orcid", "agent", "anonymous"):
        kinds = [requested]
    elif requested is None:
        kinds = ["orcid", "agent"]
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
        if kind != "agent":
            return kind, bearer.token, None
        if bearer.identity is None:
            continue
        agent_record = load_agent_key(server, bearer.identity)
        if not agent_record:
            continue
        signing = AgentSigningKey.from_private_bytes(
            handle=agent_record.handle,
            private_key_bytes=agent_record.private_key_bytes(),
        )
        return kind, bearer.token, signing
    return None


def _parse_field_pairs(pairs: list[str]) -> dict[str, Any]:
    """Parse ``--field key=value`` pairs into a dict. JSON-decode each
    value so callers can pass numbers/booleans/arrays/objects without
    extra shell-quoting tricks; fall back to plain string if not JSON."""
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            typer.secho(
                f"--field must be key=value, got {pair!r}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        k, _, v = pair.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _post_annotation(
    *,
    server: str,
    annotation: dict[str, Any],
    token: str,
    auth: AgentSigningAuth | None,
) -> tuple[int, dict[str, Any]]:
    """POST one annotation; return ``(status, body)``."""
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{server.rstrip('/')}/annotations",
            headers={"Authorization": f"Bearer {token}"},
            json=annotation,
            auth=auth,
        )
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = {"raw": resp.text}
    return resp.status_code, body


def annotation_post(
    target_id: Annotated[
        str,
        typer.Argument(
            help=(
                "ID of the paper, section, claim, figure, or annotation to "
                "annotate. For claims, use the canonical form "
                "<paper_id>:<label> (e.g. paper-abc:c1 or rrxiv:2605.00001:claim:c1)."
            ),
        ),
    ],
    annotation_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help=(
                "Annotation type: replication | contradiction | extension | "
                "erratum | summary | comment | code_link | dataset_link | "
                "claim_extraction | revision_summary | claim_retraction | "
                "paper_retraction."
            ),
        ),
    ],
    message: Annotated[
        str | None,
        typer.Option(
            "--message",
            "-m",
            help="Inline content (Markdown). Mutually exclusive with --content-file.",
        ),
    ] = None,
    content_file: Annotated[
        Path | None,
        typer.Option(
            "--content-file",
            "-F",
            exists=True,
            readable=True,
            help="Read content from a file. Mutually exclusive with --message.",
        ),
    ] = None,
    target_type: Annotated[
        str,
        typer.Option(
            "--target-type",
            help=(
                "Override the inferred target_type. Default: claim if "
                "target_id contains ':claim:' or matches <id>:cN, else paper."
            ),
        ),
    ] = "",
    fields: Annotated[
        list[str] | None,
        typer.Option(
            "--field",
            "-f",
            help=(
                "Add a key=value to structured_payload. Values are JSON-parsed "
                "(so `--field reason=\\\"superseded_by_revision\\\"` is a string, "
                "`--field count=3` is an int). Repeatable."
            ),
        ),
    ] = None,
    evidence_links: Annotated[
        list[str] | None,
        typer.Option(
            "--evidence",
            help="URI to evidence (notebook, dataset, code). Repeatable.",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        typer.Option(
            "--in-reply-to",
            help="Annotation ID this is a reply to (RRP-0018 threads).",
        ),
    ] = None,
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    identity: Annotated[
        str | None,
        typer.Option(
            "--identity",
            help="'orcid' or 'agent'. Required when multiple are stored for the same server.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the response body as raw JSON."),
    ] = False,
) -> None:
    """POST a new annotation to a paper, section, claim, figure, or other annotation.

    Examples::

        # Retract a claim, point at the surviving claim:
        rrxiv annotation post old-paper:c1 \\
            --type claim_retraction \\
            --message "Superseded by v2:c1 — see rrxiv:2605.00001:claim:c1" \\
            --field reason=\\"superseded_by_revision\\" \\
            --field superseded_by=\\"rrxiv:2605.00001:claim:c1\\"

        # Mark a paper retracted at the paper level (RRP-0007 c1):
        rrxiv annotation post some-paper-id \\
            --type paper_retraction \\
            --message "Authors withdraw the paper following data audit." \\
            --field reason=\\"data_error\\"

        # Replicate a claim:
        rrxiv annotation post some-paper:c3 \\
            --type replication \\
            --message "Independent re-run confirms the 28% figure within 2pp." \\
            --field outcome=\\"supports\\" \\
            --field reproduction_kind=\\"fresh_replication\\" \\
            --evidence https://github.com/me/replication-c3
    """
    # ---- Content ------------------------------------------------------
    if message and content_file:
        typer.secho(
            "use either --message or --content-file, not both",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not message and not content_file:
        typer.secho(
            "annotation requires --message or --content-file",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    content = message if message else content_file.read_text(encoding="utf-8")  # type: ignore[union-attr]

    # ---- Resolve identity --------------------------------------------
    resolved = _resolve_identity(server=server, requested=identity)
    if resolved is None:
        typer.secho(
            f"no stored identity for {server!r}. Run 'rrxiv login orcid' or "
            "'rrxiv login agent' first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    identity_kind, bearer_token, agent_key = resolved
    sign_auth = AgentSigningAuth(agent_key) if agent_key is not None else None

    # ---- Infer target_type if not given ------------------------------
    resolved_target_type = target_type
    if not resolved_target_type:
        # ":claim:" suffix or ":<n>" / ":c<n>" tail → claim;
        # else assume paper. Sections/figures/annotations stay explicit.
        if ":claim:" in target_id:
            resolved_target_type = "claim"
        else:
            tail = target_id.rsplit(":", 1)[-1] if ":" in target_id else ""
            if tail and (
                tail.startswith("c") or tail.lstrip("c").isdigit() or tail.isdigit()
            ):
                resolved_target_type = "claim"
            else:
                resolved_target_type = "paper"

    # ---- Resolve created_by from identity ----------------------------
    if identity_kind == "agent" and agent_key is not None:
        created_by = {
            "identity_type": "agent",
            "identity": agent_key.handle,
        }
    elif identity_kind == "orcid":
        # The server overrides this from the bearer token's claim — but
        # we set something sensible for local dry-runs and logging.
        created_by = {"identity_type": "orcid", "identity": "self"}
    else:
        created_by = {"identity_type": identity_kind, "identity": "self"}

    # ---- Build the annotation ----------------------------------------
    structured = _parse_field_pairs(fields or []) or None
    annotation: dict[str, Any] = {
        "id": f"ann-{uuid.uuid4().hex[:12]}",
        "target_id": target_id,
        "target_type": resolved_target_type,
        "annotation_type": annotation_type,
        "content": content,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_by": created_by,
    }
    if structured:
        annotation["structured_payload"] = structured
    if evidence_links:
        annotation["evidence_links"] = list(evidence_links)
    if in_reply_to:
        annotation["in_reply_to"] = in_reply_to

    # ---- POST ---------------------------------------------------------
    if not json_output:
        typer.echo(
            f"==> {annotation_type} on {resolved_target_type} {target_id}"
            f" -> {server} as {identity_kind}"
        )
    status, body = _post_annotation(
        server=server, annotation=annotation, token=bearer_token, auth=sign_auth
    )
    if json_output:
        typer.echo(json.dumps(body, indent=2, sort_keys=True))
        if status >= 400:
            raise typer.Exit(code=1)
        return
    if status >= 400:
        typer.secho(f"FAILED status={status}", fg=typer.colors.RED, err=True)
        typer.echo(json.dumps(body, indent=2), err=True)
        raise typer.Exit(code=1)
    typer.secho("annotation posted", fg=typer.colors.GREEN, bold=True)
    ann_id = body.get("id") or annotation["id"]
    typer.echo(f"  id:            {ann_id}")
    typer.echo(f"  type:          {annotation_type}")
    typer.echo(f"  target:        {resolved_target_type}/{target_id}")
    if structured:
        for k, v in structured.items():
            typer.echo(f"  payload.{k}:   {v!r}")


def annotation_list(
    target_id: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Target ID to filter by — e.g. a paper_id (paper-abc or "
                "rrxiv:2605.00001) or a claim_id (paper-abc:c1). When the "
                "target is a paper, returns annotations on the paper AND on "
                "every claim of that paper. Omit to list server-wide."
            ),
        ),
    ] = None,
    annotation_type: Annotated[
        str | None,
        typer.Option(
            "--type",
            "-t",
            help="Filter by annotation type (replication / claim_retraction / etc.).",
        ),
    ] = None,
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-n",
            help="Cap on rows shown (paged readers can re-run with cursor support later).",
        ),
    ] = 50,
) -> None:
    """List annotations, optionally scoped to a target and/or filtered by type."""
    base = server.rstrip("/")
    params: dict[str, str] = {}
    if annotation_type:
        params["annotation_type"] = annotation_type

    if target_id and (":claim:" in target_id or _looks_like_claim_id(target_id)):
        # Claim-scoped: hit /annotations?target_id=…
        url = f"{base}/annotations"
        params["target_id"] = target_id
    elif target_id:
        # Paper-scoped: server has a per-paper endpoint that includes
        # claim-level annotations.
        url = f"{base}/papers/{target_id}/annotations"
    else:
        url = f"{base}/annotations"

    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url, params=params)
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)
    body = resp.json()
    items = body.get("items", body if isinstance(body, list) else [])

    if json_output:
        typer.echo(json.dumps(items[:limit], indent=2, sort_keys=True))
        return

    if not items:
        typer.echo("(no annotations)")
        return

    suffix = f" (showing first {limit})" if len(items) > limit else ""
    typer.echo(f"{len(items)} annotation(s){suffix}")
    for ann in items[:limit]:
        atype = ann.get("annotation_type", "?")
        tgt = ann.get("target_id", "?")
        ttype = ann.get("target_type", "?")
        by = ann.get("created_by", {}) or {}
        when = (ann.get("created_at") or "")[:19]
        author = f"{by.get('identity_type','?')}:{by.get('identity','?')}"
        typer.echo(f"  {when}  {atype:18}  {ttype}/{tgt}  by {author}")
        payload = ann.get("structured_payload")
        if isinstance(payload, dict):
            for k, v in payload.items():
                typer.echo(f"      payload.{k} = {v!r}")


def _looks_like_claim_id(target_id: str) -> bool:
    """Heuristic: claim ids contain ':claim:' or end in :<digits>/c<digits>."""
    if ":claim:" in target_id:
        return True
    if ":" not in target_id:
        return False
    tail = target_id.rsplit(":", 1)[-1]
    return (tail.startswith("c") and tail[1:].isdigit()) or tail.isdigit()
