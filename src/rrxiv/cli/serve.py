"""``rrxiv serve`` — start the FastAPI reference server.

Wraps uvicorn with sensible defaults. Dev mode is on by default; turn
off for any deployment-shaped use (which would also need real ORCID
credentials and a persistent store anyway, so the default is safe).
"""

from __future__ import annotations

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
    )
    app = build_app(settings=settings)

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
