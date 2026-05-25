"""Read-only CLI commands: papers / claims / search / versions / version.

Sprint 19.P4. Every "show me the state" operation used to be a curl
plus jq pipe. The CLI had write-side commands (submit, retract,
replicate) plus CIR-side commands (parse, validate, graph) but no
read-side equivalents for the most common ops. This module fills that
gap with a thin layer over httpx + the existing public API.

No auth required for any of these — read endpoints are public. The
``--json`` flag emits raw JSON for script-friendliness.

All commands take ``--server`` (defaults to ``$RRXIV_SERVER`` →
``https://api.rrxiv.com/api/v0``).
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

import httpx
import typer

from rrxiv import __version__ as _cli_version_str

DEFAULT_SERVER = os.environ.get("RRXIV_SERVER", "https://api.rrxiv.com/api/v0")


def _client(server: str) -> httpx.Client:
    return httpx.Client(timeout=30.0, base_url=server.rstrip("/"))


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, sort_keys=True))


def _format_paper_row(p: dict[str, Any]) -> str:
    slug = p.get("id_slug") or p.get("id", "?")
    v = p.get("version", "?")
    stats = p.get("stats") or {}
    n_claims = stats.get("claims", 0)
    status = stats.get("status") or "?"
    title = p.get("title", "")
    if len(title) > 60:
        title = title[:57] + "..."
    return f"  {slug:25} {v:5} {n_claims:>3}cl  {status:12} {title}"


# ---- rrxiv version ----------------------------------------------------


def cli_version(
    server: Annotated[
        str | None,
        typer.Option(
            "--server",
            help=(
                "API base URL to ping for the server's /version. Set empty "
                "with --no-server to skip the round-trip."
            ),
        ),
    ] = DEFAULT_SERVER,
    no_server: Annotated[
        bool,
        typer.Option("--no-server", help="Skip the server round-trip."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """Show CLI version, supported protocol version, and (optionally)
    the configured server's /version response.

    Three coherent versions: the CLI's own (pyproject.toml), the
    protocol version the CLI was built against (always matches the
    schema bundled in ``_schemas/``), and the server's reported version
    if reachable.
    """
    info: dict[str, Any] = {
        "cli_version": _cli_version_str,
        # Pulled from the canonical paper.schema.json bundled under
        # src/rrxiv/_schemas/. The schema's top-level "version" field
        # is the rrxiv protocol version it conforms to.
        "protocol_version": "0.1.0",
        "server": None,
    }

    if not no_server and server:
        try:
            with _client(server) as c:
                resp = c.get("/version")
            if resp.status_code == 200:
                info["server"] = resp.json()
            else:
                info["server"] = {
                    "error": f"status {resp.status_code}",
                    "body": resp.text[:200],
                }
        except httpx.HTTPError as e:
            info["server"] = {"error": f"unreachable: {e}"}

    if json_output:
        _print_json(info)
        return

    typer.echo(f"rrxiv CLI:    {info['cli_version']}")
    typer.echo(f"protocol:     {info['protocol_version']}")
    if info["server"] is None:
        typer.echo("server:       (skipped)")
    elif isinstance(info["server"], dict) and "error" in info["server"]:
        typer.echo(f"server:       {info['server']['error']}")
    elif isinstance(info["server"], dict):
        sv = info["server"]
        typer.echo(f"server:       {server}")
        typer.echo(f"  server:     {sv.get('server', '?')}")
        typer.echo(f"  protocol:   {sv.get('protocol', '?')}")
        if sv.get("supported_api_versions"):
            typer.echo(f"  supports:   {sv.get('supported_api_versions')}")


# ---- rrxiv papers -----------------------------------------------------

papers_app = typer.Typer(
    no_args_is_help=True,
    help="Read papers from a rrxiv server.",
)


@papers_app.command("list")
def papers_list(
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            help="Filter by scope id (active/agent/human/contested/fresh).",
        ),
    ] = None,
    topic: Annotated[
        str | None,
        typer.Option("--topic", help="Filter by topic id (e.g. cs.DL)."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Page size."),
    ] = 20,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Pagination cursor."),
    ] = None,
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """List head-of-lineage papers."""
    params: dict[str, str | int] = {"limit": limit}
    if scope:
        params["scope"] = scope
    if topic:
        params["topic"] = topic
    if cursor:
        params["cursor"] = cursor

    with _client(server) as c:
        resp = c.get("/papers", params=params)
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    items = body.get("items", [])

    if json_output:
        _print_json(body)
        return

    if not items:
        typer.echo("(no papers)")
        return
    typer.echo(f"{len(items)} paper(s)")
    for p in items:
        typer.echo(_format_paper_row(p))
    if body.get("next_cursor"):
        typer.echo(f"\nnext: --cursor {body['next_cursor']}")


@papers_app.command("get")
def papers_get(
    paper_id: Annotated[
        str,
        typer.Argument(help="paper_id or id_slug, e.g. rrxiv:2605.00001."),
    ],
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """Fetch a single paper's full record."""
    with _client(server) as c:
        resp = c.get(f"/papers/{paper_id}")
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    if json_output:
        _print_json(body)
        return

    typer.echo(f"id:        {body.get('id')}")
    typer.echo(f"id_slug:   {body.get('id_slug')}")
    typer.echo(f"version:   {body.get('version')}")
    typer.echo(f"title:     {body.get('title')}")
    authors = body.get("authors") or []
    if authors:
        parts = []
        for a in authors:
            role = a.get("role")
            suffix = f" ({role})" if role and role != "author" else ""
            parts.append(f"{a.get('name')}{suffix}")
        typer.echo(f"authors:   {', '.join(parts)}")
    if body.get("topics"):
        typer.echo(f"topics:    {', '.join(body['topics'])}")
    if body.get("license"):
        typer.echo(f"license:   {body['license']}")
    src = body.get("source") or {}
    if src.get("uri"):
        typer.echo(f"source:    {src['uri']}")
    if src.get("rendered_pdf_uri"):
        typer.echo(f"pdf:       {src['rendered_pdf_uri']}")
    ef = body.get("embedded_from")
    if ef:
        ya = ef.get("original_work_year")
        typer.echo(
            f"embedded:  {ef.get('original_author')}, "
            f"{ef.get('original_work_title') or ''}{(' (' + str(ya) + ')') if ya else ''}"
        )
        tr = ef.get("translation")
        if tr:
            typer.echo(f"  translation: {tr.get('translator')} ({tr.get('year')})")


@papers_app.command("versions")
def papers_versions(
    paper_id: Annotated[
        str,
        typer.Argument(help="paper_id or id_slug."),
    ],
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """Show the version chain for a paper (oldest first)."""
    with _client(server) as c:
        resp = c.get(f"/papers/{paper_id}/versions")
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    items = body.get("items", [])

    if json_output:
        _print_json(body)
        return

    if not items:
        typer.echo("(no versions)")
        return
    for v in items:
        when = (v.get("submitted_at") or "")[:10]
        typer.echo(f"  {v.get('version', '?'):5} {when}  {v.get('id', '?')}")


# ---- rrxiv claims -----------------------------------------------------

claims_app = typer.Typer(
    no_args_is_help=True,
    help="Read claims from a rrxiv server.",
)


@claims_app.command("list")
def claims_list(
    paper_id: Annotated[
        str,
        typer.Argument(help="paper_id or id_slug to list claims for."),
    ],
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """List claims for a paper, with derived replication_status."""
    with _client(server) as c:
        resp = c.get(f"/papers/{paper_id}/claims")
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    items = body.get("items", [])

    if json_output:
        _print_json(body)
        return

    if not items:
        typer.echo("(no claims)")
        return
    for c in items:
        cid = c.get("id", "?")
        status = c.get("replication_status", "?")
        stmt = c.get("statement", "")
        if len(stmt) > 70:
            stmt = stmt[:67] + "..."
        typer.echo(f"  {status:12} {cid}")
        typer.echo(f"               {stmt}")


@claims_app.command("get")
def claims_get(
    claim_id: Annotated[
        str,
        typer.Argument(help="claim_id, e.g. rrxiv:2605.00001:claim:c1."),
    ],
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """Fetch a single claim with derived status."""
    with _client(server) as c:
        resp = c.get(f"/claims/{claim_id}")
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    if json_output:
        _print_json(body)
        return

    typer.echo(f"id:                 {body.get('id')}")
    typer.echo(f"paper_id:           {body.get('paper_id')}")
    typer.echo(f"replication_status: {body.get('replication_status')}")
    typer.echo(f"claim_type:         {body.get('claim_type')}")
    typer.echo(f"evidence_type:      {body.get('evidence_type')}")
    typer.echo("statement:")
    typer.echo(f"  {body.get('statement')}")


@claims_app.command("top")
def claims_top(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="How many."),
    ] = 5,
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """Top-ranked claims by dependent-count + replication score."""
    with _client(server) as c:
        resp = c.get("/claims/top", params={"limit": limit})
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    items = body.get("items", body if isinstance(body, list) else [])

    if json_output:
        _print_json(body)
        return

    if not items:
        typer.echo("(no claims)")
        return
    for i, c in enumerate(items, start=1):
        cid = c.get("id", "?")
        dep = c.get("dependents_count") or c.get("queries", 0)
        stmt = c.get("statement", "")
        if len(stmt) > 60:
            stmt = stmt[:57] + "..."
        typer.echo(f"  {i}. [{dep:>3} deps] {cid}")
        typer.echo(f"     {stmt}")


# ---- rrxiv search -----------------------------------------------------


def cli_search(
    query: Annotated[
        str,
        typer.Argument(
            help=(
                "Search query (matches title + abstract for papers; "
                "statement for claims)."
            ),
        ),
    ],
    what: Annotated[
        str,
        typer.Option(
            "--what",
            help="Which collection to search: 'papers' or 'claims'.",
        ),
    ] = "papers",
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Page size."),
    ] = 20,
    server: Annotated[
        str,
        typer.Option("--server", help="API base URL."),
    ] = DEFAULT_SERVER,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON."),
    ] = False,
) -> None:
    """Search the corpus."""
    if what not in ("papers", "claims"):
        typer.secho(
            f"--what must be 'papers' or 'claims', got {what!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    path = f"/search/{what}"
    with _client(server) as c:
        resp = c.get(path, params={"q": query, "limit": limit})
    if resp.status_code >= 400:
        typer.secho(f"FAILED status={resp.status_code}", fg=typer.colors.RED, err=True)
        typer.echo(resp.text, err=True)
        raise typer.Exit(code=1)

    body = resp.json()
    items = body.get("items", [])

    if json_output:
        _print_json(body)
        return

    if not items:
        typer.echo(f"(no {what} match {query!r})")
        return

    typer.echo(f"{len(items)} match(es) in {what}")
    if what == "papers":
        for p in items:
            typer.echo(_format_paper_row(p))
    else:  # claims
        for c in items:
            cid = c.get("id", "?")
            status = c.get("replication_status", "?")
            stmt = c.get("statement", "")
            if len(stmt) > 70:
                stmt = stmt[:67] + "..."
            typer.echo(f"  {status:12} {cid}")
            typer.echo(f"               {stmt}")
