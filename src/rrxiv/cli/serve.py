"""``rrxiv serve`` — start the FastAPI reference server.

Wraps uvicorn with sensible defaults. Dev mode is on by default; turn
off for any deployment-shaped use (which would also need real ORCID
credentials and a persistent store anyway, so the default is safe).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port.")] = 8000,
    dev_mode: Annotated[
        bool,
        typer.Option(
            "--dev-mode/--no-dev-mode",
            help=(
                "Stub ORCID + hCaptcha (real Ed25519 stays on). "
                "Default ON for the reference server."
            ),
        ),
    ] = True,
    reload: Annotated[
        bool, typer.Option("--reload/--no-reload", help="Auto-reload on file changes.")
    ] = False,
    store: Annotated[
        str,
        typer.Option(
            "--store",
            help=(
                "Storage backend URL (RRP-0011). "
                "memory:// (default) or sqlite:///path/to/db.sqlite."
            ),
        ),
    ] = "memory://",
    seed_dir: Annotated[
        Path | None,
        typer.Option(
            "--seed-dir",
            help=(
                "Directory containing *.cir.json files. If set and the "
                "store has zero papers at boot, the directory is loaded "
                "via the same code path as `rrxiv seed-store`."
            ),
        ),
    ] = None,
) -> None:
    """Start the FastAPI reference server."""
    try:
        import uvicorn

        from rrxiv.server import build_app
        from rrxiv.server.settings import ServerSettings
    except ImportError as e:
        typer.secho(
            f"rrxiv serve needs the [server] extra: {e}. "
            "Install with: pip install 'rrxiv[server]'",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2) from e

    settings = ServerSettings(
        api_base=f"http://{host}:{port}/api/v0",
        dev_mode=dev_mode,
        store_url=store,
    )
    app = build_app(settings=settings)

    # Optional first-boot seed. Skipped if the store is already non-empty
    # — idempotent across restarts.
    if seed_dir is not None:
        if not seed_dir.is_dir():
            typer.secho(
                f"WARNING: --seed-dir {seed_dir} not found; skipping seed.",
                fg=typer.colors.YELLOW,
            )
        elif app.state.store.list_papers():
            typer.secho(
                f"Store already has papers; skipping --seed-dir {seed_dir}.",
                fg=typer.colors.YELLOW,
            )
        else:
            # Delegate to the shared loader so this path gets the
            # same source.uri / rendered_pdf_uri rewriting and edge
            # canonicalisation that ``rrxiv seed-store`` does.
            from rrxiv.cli.seed import _iter_cir_files, load_cir_into_store

            files = _iter_cir_files(seed_dir)
            seeded_papers = 0
            for cir_path in files:
                result = load_cir_into_store(
                    cir_path, app.state.store, quiet=True
                )
                if result is not None:
                    seeded_papers += 1
            typer.secho(
                f"Seeded {seeded_papers} paper(s) from {seed_dir}.",
                fg=typer.colors.GREEN,
            )

    typer.secho(
        f"\nStarting rrxiv reference server on http://{host}:{port}",
        fg=typer.colors.GREEN,
    )
    typer.echo("  /api/v0/docs    — Swagger UI")
    typer.echo("  /api/v0/redoc   — ReDoc UI")
    if dev_mode:
        typer.secho(
            "  (dev mode: ORCID + hCaptcha stubbed; not for production)",
            fg=typer.colors.YELLOW,
        )
    typer.echo("")

    uvicorn.run(app, host=host, port=port, reload=reload)


__all__ = ["serve"]
