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
from rrxiv.server.metrics import metrics_endpoint
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


def _sentry_before_send(event: Any, hint: dict[str, Any]) -> Any:
    """Filter handled-4xx noise out of Sentry.

    The protocol surface raises ``ProblemError`` for expected user
    failures (404 not found, 422 bad payload, 429 rate-limited);
    those are caught by ``install_exception_handlers`` and never
    propagate as uncaught exceptions. But pydantic's
    ``RequestValidationError`` *can* surface as an uncaught
    exception in some auto-instrumentation paths, and Starlette
    occasionally re-raises HTTPException through the middleware
    chain. Drop those.

    Returns the event (forward to Sentry) or ``None`` (drop). The
    filter is intentionally narrow: anything 5xx — or any uncaught
    Python exception — gets through.
    """
    exc = hint.get("exc_info")
    if exc is not None:
        exc_type = exc[0]
        # Pydantic v2 validation errors (FastAPI body parsing)
        if exc_type.__name__ == "RequestValidationError":
            return None
        # Our own ProblemError carries the status; drop the 4xx
        # variants. 5xx ProblemErrors are an oddity worth seeing.
        if exc_type.__name__ == "ProblemError":
            instance = exc[1]
            status = getattr(instance, "status", 500)
            if 400 <= status < 500:
                return None
        # Starlette/FastAPI HTTPException with 4xx
        if exc_type.__name__ == "HTTPException":
            status = getattr(exc[1], "status_code", 500)
            if 400 <= status < 500:
                return None
    return event


def _init_sentry() -> None:
    """Initialise Sentry if SENTRY_DSN is set.

    No-op if the env var is missing or sentry-sdk isn't installed.
    Safe to call multiple times — guarded by a module-level flag so
    repeated ``build_app`` calls (tests) don't re-init.

    Sprint 21 hardening:
      - ``before_send`` filter drops handled 4xx noise.
      - Deploy provenance tags (``fly_machine_id``, ``fly_region``,
        ``rrxiv_commit_sha``) attached to the default scope.
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
        before_send=_sentry_before_send,
    )
    # Deploy-provenance tags — present on every event captured by
    # this process. Cardinality is bounded (one machine per pod).
    fly_machine = os.environ.get("FLY_MACHINE_ID")
    fly_region = os.environ.get("FLY_REGION")
    commit_sha = os.environ.get("RRXIV_COMMIT_SHA")
    if fly_machine:
        sentry_sdk.set_tag("fly_machine_id", fly_machine)
    if fly_region:
        sentry_sdk.set_tag("fly_region", fly_region)
    if commit_sha:
        sentry_sdk.set_tag("rrxiv_commit_sha", commit_sha[:12])
    sentry_sdk.set_tag("rrxiv_version", rrxiv_version)
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

    # Sprint 21: Prometheus exposition at the root, outside /api/v0.
    # Fly's `[metrics]` block scrapes this every 15s; the JSON access
    # log middleware skips /metrics so the scrape doesn't spam logs.
    app.add_route("/metrics", metrics_endpoint, methods=["GET"])

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
