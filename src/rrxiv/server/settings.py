"""Server settings — env-driven config (RRP-0008)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ServerSettings:
    """Runtime configuration for the reference server.

    All fields have sensible dev-mode defaults so ``rrxiv serve``
    works out of the box. Production deployments override via env
    vars or a config file (the CLI loads from env by default).
    """

    api_base: str = "http://127.0.0.1:8000/api/v0"
    """The public-facing base URL the server is reachable at. Used
    in OAuth redirect_uri construction and OpenAPI docs links."""

    store_url: str = "memory://"
    """Storage backend selection (RRP-0011). ``memory://`` (default)
    uses an in-memory store. ``sqlite:///path/to/db.sqlite`` uses
    SQLite at the given path. Future schemes (e.g. ``postgres://``)
    are RRP-future."""

    dev_mode: bool = True
    """When True, ORCID OAuth and hCaptcha are stubbed (any code/
    response is accepted, dev iDs returned). Real Ed25519 verification
    stays on. Default True for the development reference server.
    Set False for any deployment-shaped use."""

    orcid_client_id: str | None = None
    orcid_client_secret: str | None = None
    orcid_authorize_url: str = "https://orcid.org/oauth/authorize"
    orcid_token_url: str = "https://orcid.org/oauth/token"
    orcid_redirect_uri: str | None = None
    """The redirect_uri registered with ORCID for ``orcid_client_id``.
    Required for the paste-back render flow in production; the server
    sends it to ORCID's token endpoint as part of code exchange."""
    orcid_dev_id: str = "0000-0001-0000-DEV1"
    """ORCID iD returned in dev mode."""

    hcaptcha_secret: str | None = None
    hcaptcha_site_key: str = "10000000-ffff-ffff-ffff-000000000001"
    """A test hCaptcha site key. dev_mode bypasses verification."""

    signature_clock_skew_seconds: int = 300
    """RFC 9421 created tolerance window per RRP-0007."""

    rate_limit_anonymous_read_rpm: int = 120
    rate_limit_orcid_read_rpm: int = 240
    rate_limit_agent_read_rpm: int = 600
    rate_limit_orcid_write_rpm: int = 30
    rate_limit_agent_write_rpm: int = 30

    challenge_ttl_seconds: int = 300
    """Anonymous challenge lifetime."""

    token_ttl_seconds_orcid: int = 3600 * 24
    token_ttl_seconds_agent: int = 3600 * 24 * 30
    token_ttl_seconds_anonymous: int = 3600

    idempotency_window_seconds: int = 86400
    """How long a write Idempotency-Key is remembered."""

    enable_cors: bool = True
    """If True, attach a CORS middleware on read endpoints."""

    cors_origins: tuple[str, ...] = ()
    """Allowlist of origins for the CORS middleware. Empty tuple = allow
    ``*`` (suitable for dev_mode). In production set to the deployed web
    origin(s) via ``RRXIV_CORS_ORIGINS=https://rrxiv.org,https://www.rrxiv.org``."""

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    metadata: dict[str, str] = field(default_factory=dict)
    """Free-form labels surfaced in /version. Useful for instance
    identification ("instance": "rrxiv.com"). Not part of the protocol;
    kept lightweight for now."""

    @classmethod
    def from_env(cls, *, environ: dict[str, str] | None = None) -> ServerSettings:
        """Build settings from process env vars, with the field
        defaults as fallback. Vars are upper-cased with a ``RRXIV_``
        prefix (e.g. ``RRXIV_DEV_MODE=0``).

        Truthy parsing for booleans: ``1/true/yes/on`` → True;
        anything else → False.
        """
        import os

        env = environ if environ is not None else os.environ

        def get_str(name: str, default: str | None) -> str | None:
            return env.get(f"RRXIV_{name}", default)

        def get_str_required(name: str, default: str) -> str:
            return env.get(f"RRXIV_{name}", default)

        def get_bool(name: str, default: bool) -> bool:
            v = env.get(f"RRXIV_{name}")
            if v is None:
                return default
            return v.lower() in ("1", "true", "yes", "on")

        def get_int(name: str, default: int) -> int:
            v = env.get(f"RRXIV_{name}")
            return int(v) if v is not None else default

        return cls(
            api_base=get_str_required("API_BASE", cls.api_base),
            store_url=get_str_required("STORE_URL", cls.store_url),
            dev_mode=get_bool("DEV_MODE", cls.dev_mode),
            orcid_client_id=get_str("ORCID_CLIENT_ID", cls.orcid_client_id),
            orcid_client_secret=get_str(
                "ORCID_CLIENT_SECRET", cls.orcid_client_secret
            ),
            orcid_authorize_url=get_str_required(
                "ORCID_AUTHORIZE_URL", cls.orcid_authorize_url
            ),
            orcid_token_url=get_str_required("ORCID_TOKEN_URL", cls.orcid_token_url),
            orcid_redirect_uri=get_str(
                "ORCID_REDIRECT_URI", cls.orcid_redirect_uri
            ),
            orcid_dev_id=get_str_required("ORCID_DEV_ID", cls.orcid_dev_id),
            hcaptcha_secret=get_str("HCAPTCHA_SECRET", cls.hcaptcha_secret),
            hcaptcha_site_key=get_str_required(
                "HCAPTCHA_SITE_KEY", cls.hcaptcha_site_key
            ),
            signature_clock_skew_seconds=get_int(
                "SIGNATURE_CLOCK_SKEW_SECONDS", cls.signature_clock_skew_seconds
            ),
            rate_limit_anonymous_read_rpm=get_int(
                "RATE_LIMIT_ANONYMOUS_READ_RPM",
                cls.rate_limit_anonymous_read_rpm,
            ),
            rate_limit_orcid_read_rpm=get_int(
                "RATE_LIMIT_ORCID_READ_RPM", cls.rate_limit_orcid_read_rpm
            ),
            rate_limit_agent_read_rpm=get_int(
                "RATE_LIMIT_AGENT_READ_RPM", cls.rate_limit_agent_read_rpm
            ),
            rate_limit_orcid_write_rpm=get_int(
                "RATE_LIMIT_ORCID_WRITE_RPM", cls.rate_limit_orcid_write_rpm
            ),
            rate_limit_agent_write_rpm=get_int(
                "RATE_LIMIT_AGENT_WRITE_RPM", cls.rate_limit_agent_write_rpm
            ),
            challenge_ttl_seconds=get_int(
                "CHALLENGE_TTL_SECONDS", cls.challenge_ttl_seconds
            ),
            token_ttl_seconds_orcid=get_int(
                "TOKEN_TTL_SECONDS_ORCID", cls.token_ttl_seconds_orcid
            ),
            token_ttl_seconds_agent=get_int(
                "TOKEN_TTL_SECONDS_AGENT", cls.token_ttl_seconds_agent
            ),
            token_ttl_seconds_anonymous=get_int(
                "TOKEN_TTL_SECONDS_ANONYMOUS", cls.token_ttl_seconds_anonymous
            ),
            idempotency_window_seconds=get_int(
                "IDEMPOTENCY_WINDOW_SECONDS", cls.idempotency_window_seconds
            ),
            enable_cors=get_bool("ENABLE_CORS", cls.enable_cors),
            cors_origins=tuple(
                o.strip()
                for o in (get_str_required("CORS_ORIGINS", "") or "").split(",")
                if o.strip()
            ),
        )
