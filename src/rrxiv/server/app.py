"""FastAPI app construction (RRP-0008)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI

from rrxiv import __version__ as rrxiv_version
from rrxiv.server.annotations.router import router as annotations_router
from rrxiv.server.auth.router import router as auth_router
from rrxiv.server.claims.router import router as claims_router
from rrxiv.server.errors import install_exception_handlers
from rrxiv.server.papers.router import router as papers_router
from rrxiv.server.settings import ServerSettings
from rrxiv.server.snapshots.router import router as snapshots_router
from rrxiv.server.store import MemoryStore, Store

PROTOCOL_VERSION = "0.1.0"
API_PREFIX = "/api/v0"


def build_app(
    *,
    settings: ServerSettings | None = None,
    store: Store | None = None,
) -> FastAPI:
    """Construct a FastAPI app with all routes mounted under /api/v0.

    Args:
        settings: optional :class:`ServerSettings`. Defaults to
            ``ServerSettings.from_env()`` (env-driven), which itself
            defaults to dev mode.
        store: optional :class:`Store`. Defaults to a fresh
            :class:`MemoryStore`.
    """
    settings = settings or ServerSettings.from_env()
    store = store or MemoryStore()

    app = FastAPI(
        title="rrxiv reference server",
        version=rrxiv_version,
        description=(
            "FastAPI reference implementation of the rrxiv protocol "
            f"v{PROTOCOL_VERSION}. **In-memory storage; not for production.** "
            "See RRP-0008."
        ),
        openapi_url=API_PREFIX + "/openapi.json",
        docs_url=API_PREFIX + "/docs",
        redoc_url=API_PREFIX + "/redoc",
    )

    # State.
    app.state.settings = settings
    app.state.store = store

    install_exception_handlers(app)

    # Mount versioned API.
    api = APIRouter()

    @api.get("/version", tags=["System"])
    def version() -> dict[str, Any]:
        return {
            "server": f"rrxiv-python-server/{rrxiv_version}",
            "protocol": PROTOCOL_VERSION,
            "supported_api_versions": ["v0"],
        }

    api.include_router(auth_router)
    api.include_router(papers_router)
    api.include_router(claims_router)
    api.include_router(annotations_router)
    api.include_router(snapshots_router)
    app.include_router(api, prefix=API_PREFIX)

    if settings.enable_cors:
        from fastapi.middleware.cors import CORSMiddleware

        # Read endpoints are documented as CORS-permissive in the spec.
        # Writes require the bearer + Idempotency-Key, which makes them
        # CORS-safe enough; in v0.1 we allow * on all origins. Production
        # deployments may want to tighten.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    return app
