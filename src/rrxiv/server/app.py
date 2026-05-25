"""FastAPI app construction (RRP-0008)."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, FastAPI

from rrxiv import __version__ as rrxiv_version
from rrxiv.server.annotations.router import router as annotations_router
from rrxiv.server.auth.router import router as auth_router
from rrxiv.server.auth.signature_middleware import (
    SignatureVerificationMiddleware,
)
from rrxiv.server.authors.router import router as authors_router
from rrxiv.server.claims.router import router as claims_router
from rrxiv.server.discovery.router import router as discovery_router
from rrxiv.server.errors import install_exception_handlers
from rrxiv.server.observability import RequestLoggingMiddleware
from rrxiv.server.papers.router import router as papers_router
from rrxiv.server.search.router import router as search_router
from rrxiv.server.settings import ServerSettings
from rrxiv.server.snapshots.router import router as snapshots_router
from rrxiv.server.stats.router import router as stats_router
from rrxiv.server.store import Store, store_from_url
from rrxiv.server.submissions.router import (
    router as submissions_router,
)
from rrxiv.server.submissions.router import (
    sources_router,
)

PROTOCOL_VERSION = "0.1.0"
API_PREFIX = "/api/v0"

_log = logging.getLogger(__name__)
_sentry_initialised = False


def _init_sentry() -> None:
    """Initialise Sentry if SENTRY_DSN is set.

    No-op if the env var is missing or sentry-sdk isn't installed.
    Safe to call multiple times — guarded by a module-level flag so
    repeated ``build_app`` calls (tests) don't re-init.
    """
    global _sentry_initialised
    if _sentry_initialised:
        return
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - optional dep
        _log.warning("SENTRY_DSN set but sentry-sdk not installed; skipping init")
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE", f"rrxiv-python@{rrxiv_version}"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
    )
    _sentry_initialised = True
    _log.info("Sentry initialised (env=%s, release=%s)",
              os.environ.get("SENTRY_ENVIRONMENT", "production"),
              os.environ.get("SENTRY_RELEASE", f"rrxiv-python@{rrxiv_version}"))


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
    store = store or store_from_url(settings.store_url)

    # Initialise error reporting before app construction so any error
    # raised during startup is captured. No-ops if SENTRY_DSN unset.
    _init_sentry()

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

    # RRP-0007: signature middleware runs before FastAPI body parsing
    # so multipart routes (POST /submissions) can verify the signature
    # and still let FastAPI re-parse the body.
    app.add_middleware(SignatureVerificationMiddleware)

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
    api.include_router(sources_router)
    api.include_router(submissions_router)
    api.include_router(claims_router)
    api.include_router(annotations_router)
    api.include_router(snapshots_router)
    api.include_router(search_router)
    api.include_router(discovery_router)
    api.include_router(stats_router)
    api.include_router(authors_router)
    app.include_router(api, prefix=API_PREFIX)

    # Sprint 20 observability: structured JSON access log + identity
    # context on every request. Toggle via RRXIV_LOG_FORMAT=json. Cheap
    # enough to keep on always; the middleware skips /metrics + the
    # 30-second Fly healthcheck on /api/v0/version to avoid log noise.
    app.add_middleware(RequestLoggingMiddleware)

    if settings.enable_cors:
        from fastapi.middleware.cors import CORSMiddleware

        # Read endpoints are documented as CORS-permissive in the spec.
        # Writes require the bearer + Idempotency-Key, which makes them
        # CORS-safe enough. Production deployments set
        # ``RRXIV_CORS_ORIGINS=https://rrxiv.org,...`` to lock the
        # allowlist down; dev mode defaults to ``*``.
        allow_origins = (
            list(settings.cors_origins) if settings.cors_origins else ["*"]
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    return app
