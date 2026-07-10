"""``rrxiv login`` subcommands per RRP-0006.

Subcommands:

- ``rrxiv login orcid``     — OAuth dance (loopback, paste-fallback)
- ``rrxiv login agent``     — Ed25519 enrollment
- ``rrxiv login anonymous`` — hCaptcha-style challenge → token
- ``rrxiv login status``    — show stored identities for a server
- ``rrxiv logout``          — clear stored credentials

Token storage delegates to :mod:`rrxiv.cli.credentials`.
"""

from __future__ import annotations

import base64
import http.server
import os
import socket
import time
import urllib.parse
import webbrowser
from threading import Thread
from typing import Annotated, Any

import typer

from rrxiv.cli.credentials import (
    StoredAgentKey,
    StoredBearer,
    delete_agent_key,
    delete_bearer,
    delete_orcid_key,
    load_bearer,
    store_agent_key,
    store_bearer,
    stored_identities_for_server,
    stored_servers,
)

DEFAULT_SERVER_ENV = "RRXIV_API_BASE"
# The API is served from the api. subdomain; the apex (rrxiv.com) is the web
# client and 404s /api/v0. Matches cli/read_commands.py's default.
DEFAULT_SERVER = "https://api.rrxiv.com/api/v0"


login_app = typer.Typer(
    no_args_is_help=True,
    help="Mint and manage rrxiv API tokens (RRP-0005, RRP-0006).",
)


# ----------------------------- shared helpers -----------------------------


def _resolved_server(server: str | None) -> str:
    if server:
        return server.rstrip("/")
    return os.environ.get(DEFAULT_SERVER_ENV, DEFAULT_SERVER).rstrip("/")


def _print_success(message: str) -> None:
    typer.secho(message, fg=typer.colors.GREEN)


def _print_warn(message: str) -> None:
    typer.secho(message, fg=typer.colors.YELLOW)


def _print_error(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)


def _now() -> int:
    return int(time.time())


# ----------------------------- ORCID OAuth -----------------------------


def _ephemeral_port() -> int:
    """Bind to 127.0.0.1:0 to get an OS-assigned port, then release it.

    There is a tiny race here (the port could be re-used by another
    process between the bind release and our actual listen). RFC 8252
    accepts this — the alternative is keeping the socket bound, which
    the stdlib http.server doesn't make easy. The race is rare in
    practice on developer machines.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


class _OAuthCallbackResult:
    """Mutable shared state between the HTTP handler thread and the
    main CLI thread."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None


def _make_handler(result: _OAuthCallbackResult) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        # Silence the default access logging.
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if "error" in qs:
                result.error = qs["error"][0]
            else:
                result.code = qs.get("code", [None])[0]
                result.state = qs.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                b"<!doctype html><html><body>"
                b"<h2>rrxiv login complete</h2>"
                b"<p>You can close this window and return to the terminal.</p>"
                b"</body></html>"
            )
            self.wfile.write(body)

    return Handler


def _start_loopback_server(
    port: int, result: _OAuthCallbackResult, *, timeout: float
) -> tuple[http.server.HTTPServer, Thread]:
    """Start the HTTP server in a background thread and return both so
    the caller can join after kicking off the browser."""
    handler = _make_handler(result)
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    server.timeout = 0.5

    def _serve() -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            server.handle_request()
            if result.code or result.error:
                return

    thread = Thread(target=_serve, daemon=True)
    thread.start()
    return server, thread


@login_app.command("orcid")
def login_orcid(
    server: Annotated[
        str | None,
        typer.Option(
            "--server",
            help="rrxiv API base URL. Defaults to $RRXIV_API_BASE or rrxiv.com.",
        ),
    ] = None,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Use the paste-back flow instead of a local listener."),
    ] = False,
    timeout_seconds: Annotated[
        int, typer.Option("--timeout", help="Seconds to wait for the OAuth callback.")
    ] = 300,
) -> None:
    """Run the ORCID OAuth flow and persist the resulting token."""
    api_base = _resolved_server(server)

    if no_browser:
        _login_orcid_paste(api_base)
        return

    from rrxiv.auth import build_orcid_authorization_url, exchange_orcid_code

    port = _ephemeral_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    auth_url = build_orcid_authorization_url(
        api_base=api_base, redirect_uri=redirect_uri
    )

    result = _OAuthCallbackResult()
    listener, listener_thread = _start_loopback_server(
        port, result, timeout=float(timeout_seconds)
    )

    typer.echo(f"Opening browser for ORCID OAuth (server: {api_base})...")
    typer.echo(f"  redirect_uri: {redirect_uri}")
    if not webbrowser.open(auth_url.url):
        _print_warn(
            "couldn't auto-open the browser; visit this URL manually:\n  "
            + auth_url.url
        )

    listener_thread.join()
    listener.server_close()

    if result.error:
        _print_error(f"OAuth provider returned error: {result.error}")
        raise typer.Exit(2)
    if not result.code:
        _print_error("timed out waiting for OAuth callback; try --no-browser")
        raise typer.Exit(2)
    if result.state != auth_url.state:
        _print_error(
            "OAuth state mismatch (possible CSRF). Aborting without "
            "exchanging the code."
        )
        raise typer.Exit(2)

    bearer = exchange_orcid_code(
        api_base=api_base,
        code=result.code,
        state=result.state,
        expected_state=auth_url.state,
        # OAuth (RFC 6749 §4.1.3): the token exchange must send the SAME
        # redirect_uri we authorized with, or ORCID 401s the exchange.
        redirect_uri=redirect_uri,
    )
    _persist_bearer(api_base, bearer, expires_in_seconds=3600 * 24)
    _print_success(f"Logged in as ORCID {bearer.identity}")


def _login_orcid_paste(api_base: str) -> None:
    """Paste-fallback flow: print URL, prompt user to paste a code."""
    from rrxiv.auth import build_orcid_authorization_url

    # We authorize with the server's /auth/orcid/render endpoint as the
    # redirect_uri; ORCID redirects the browser there and the SERVER does
    # the code→iD token exchange (it threads this same render URL as the
    # redirect_uri per OAuth RFC 6749 §4.1.3). The CLI only redeems the
    # short paste code below, so it sends no redirect_uri of its own.
    auth_url = build_orcid_authorization_url(
        api_base=api_base,
        redirect_uri=f"{api_base}/auth/orcid/render",
    )
    typer.echo("Open this URL in any browser:")
    typer.echo(f"  {auth_url.url}")
    typer.echo()
    code = typer.prompt("Paste the code shown on the rrxiv page")
    code = code.strip()
    if not code:
        _print_error("empty code")
        raise typer.Exit(2)

    import httpx

    resp = httpx.post(
        f"{api_base}/auth/orcid/exchange-paste",
        json={"code": code},
        timeout=30.0,
    )
    if resp.status_code != 200:
        _print_error(
            f"server rejected paste code (status {resp.status_code}): "
            f"{resp.text[:200]}"
        )
        raise typer.Exit(2)
    body = resp.json()
    bearer = StoredBearer(
        api_base=api_base,
        identity_type="orcid",
        identity=body.get("orcid_id"),
        token=body["token"],
        issued_at_unix=_now(),
        expires_at_unix=_now() + int(body.get("expires_in_seconds") or 0),
    )
    store_bearer(bearer)
    _print_success(f"Logged in as ORCID {bearer.identity}")


# ----------------------------- agent enrollment -----------------------------


@login_app.command("agent")
def login_agent(
    handle: Annotated[
        str,
        typer.Option(
            "--handle",
            "-H",
            prompt="Agent handle (must start with @)",
            help="Agent handle, e.g. @my-extractor.",
        ),
    ],
    contact: Annotated[
        str | None,
        typer.Option(
            "--contact", help="Optional ops contact email."
        ),
    ] = None,
    server: Annotated[
        str | None, typer.Option("--server")
    ] = None,
) -> None:
    """Generate an Ed25519 keypair, enroll, and persist the bearer + key."""
    api_base = _resolved_server(server)

    try:
        from rrxiv.auth import (
            AgentEnrollmentRequest,
            enroll_agent,
        )
        from rrxiv.auth.agent import (
            build_enrollment_payload,
            sign_enrollment_payload,
        )
        from rrxiv.client.signatures import AgentSigningKey
    except ImportError as e:
        _print_error(
            f"agent enrollment needs the [agent] extra: {e}. "
            "Install with: pip install 'rrxiv[agent]'"
        )
        raise typer.Exit(2) from e

    if not handle.startswith("@"):
        _print_error("handle must start with @")
        raise typer.Exit(2)

    typer.echo(f"Generating Ed25519 keypair for {handle}...")
    signing = AgentSigningKey.generate(handle=handle)
    pub_b64 = base64.standard_b64encode(signing.public_key_bytes()).decode("ascii")
    payload = build_enrollment_payload(
        handle=handle,
        public_key_b64=pub_b64,
        issued_at=_now(),
    )
    sig_b64 = sign_enrollment_payload(
        payload=payload, private_key_bytes=signing.private_key_bytes()
    )
    request = AgentEnrollmentRequest(
        handle=handle,
        public_key_b64=pub_b64,
        payload_b64=base64.standard_b64encode(payload).decode("ascii"),
        signature_b64=sig_b64,
        contact=contact,
    )
    typer.echo(f"Enrolling against {api_base}...")
    bearer = enroll_agent(api_base=api_base, request=request)

    _persist_bearer(api_base, bearer, expires_in_seconds=86400 * 30)
    store_agent_key(
        StoredAgentKey(
            api_base=api_base,
            handle=handle,
            private_key_b64=base64.standard_b64encode(
                signing.private_key_bytes()
            ).decode("ascii"),
        )
    )
    _print_success(f"Enrolled and logged in as {handle}.")


# ----------------------------- anonymous attestation -----------------------------


@login_app.command("anonymous")
def login_anonymous(
    server: Annotated[
        str | None, typer.Option("--server")
    ] = None,
) -> None:
    """Solve a server-issued challenge (paste-back) and persist a token."""
    api_base = _resolved_server(server)

    from rrxiv.auth import (
        AnonymousChallengeResponse,
        request_anonymous_challenge,
        verify_anonymous_challenge,
    )

    challenge = request_anonymous_challenge(api_base=api_base)
    typer.echo(f"Got a {challenge.challenge_type} challenge from {api_base}.")
    typer.echo()
    typer.echo("Open this URL to solve the challenge:")
    typer.echo(
        f"  {api_base}/auth/anonymous/render"
        f"?challenge_id={challenge.challenge_id}"
        f"&site_key={challenge.site_key}"
    )
    typer.echo()
    response = typer.prompt("Paste the resulting token")
    bearer = verify_anonymous_challenge(
        api_base=api_base,
        response=AnonymousChallengeResponse(
            challenge_id=challenge.challenge_id, response=response.strip()
        ),
    )
    _persist_bearer(api_base, bearer, expires_in_seconds=3600)
    _print_success("Logged in anonymously.")


# ----------------------------- status / logout -----------------------------


@login_app.command("status")
def login_status(
    server: Annotated[str | None, typer.Option("--server")] = None,
    all_servers: Annotated[
        bool, typer.Option("--all", help="Show identities for all known servers.")
    ] = False,
) -> None:
    """Show stored identities for the configured server(s)."""
    if all_servers:
        servers = stored_servers()
        if not servers:
            typer.echo("No stored credentials.")
            return
        for s in servers:
            _print_status_for_server(s)
        return
    api_base = _resolved_server(server)
    _print_status_for_server(api_base)


def _print_status_for_server(api_base: str) -> None:
    typer.echo(f"Server: {api_base}")
    info = stored_identities_for_server(api_base)
    if not info:
        typer.echo("  (no credentials stored)")
        return
    for identity_type in ("orcid", "agent", "anonymous"):
        if identity_type == "agent":
            agent_keys: dict[str, Any] = info.get("agent_keys", {})
            bearer = load_bearer(api_base, "agent")
            if not (bearer or agent_keys):
                continue
            handle = bearer.identity if bearer else next(iter(agent_keys), "?")
            keyed = "yes" if agent_keys else "no"
            expires = _expiry_str(bearer)
            typer.echo(f"  Agent:    {handle:<24} (private key: {keyed}; {expires})")
            continue
        bearer = load_bearer(api_base, identity_type)
        if bearer is None:
            continue
        ident = bearer.identity or "—"
        expires = _expiry_str(bearer)
        typer.echo(
            f"  {identity_type.upper():<8}  {ident:<24} ({expires})"
        )


def _expiry_str(bearer: StoredBearer | None) -> str:
    if bearer is None or bearer.expires_at_unix is None:
        return "no expiry recorded"
    delta = bearer.expires_at_unix - _now()
    if delta <= 0:
        return "EXPIRED"
    if delta < 3600:
        return f"{delta // 60}m left"
    if delta < 86400:
        return f"{delta // 3600}h {(delta % 3600) // 60}m left"
    return f"{delta // 86400}d left"


@login_app.command("logout")
def logout(
    server: Annotated[str | None, typer.Option("--server")] = None,
    identity_type: Annotated[
        str | None,
        typer.Option("--identity", help="Only one of orcid|agent|anonymous."),
    ] = None,
    all_servers: Annotated[
        bool, typer.Option("--all", help="Forget every server's credentials.")
    ] = False,
) -> None:
    """Forget stored credentials."""
    if all_servers:
        for s in stored_servers():
            _logout_server(s, identity_type=None)
        _print_success("Cleared all stored credentials.")
        return
    api_base = _resolved_server(server)
    _logout_server(api_base, identity_type=identity_type)
    _print_success(f"Cleared credentials for {api_base}.")


def _logout_server(api_base: str, *, identity_type: str | None) -> None:
    if identity_type is None:
        # Wipe all identity types. Capture the orcid_id before deleting the
        # bearer so we can also drop the bound signing key (RRP-0024).
        orcid_bearer = load_bearer(api_base, "orcid")
        for it in ("orcid", "agent", "anonymous"):
            delete_bearer(api_base, it)
        info = stored_identities_for_server(api_base)
        for handle in list(info.get("agent_keys", {})):
            delete_agent_key(api_base, handle)
        _forget_orcid_keys(api_base, orcid_bearer, info)
        return
    if identity_type == "orcid":
        orcid_bearer = load_bearer(api_base, "orcid")
        delete_bearer(api_base, "orcid")
        _forget_orcid_keys(api_base, orcid_bearer, stored_identities_for_server(api_base))
        return
    delete_bearer(api_base, identity_type)  # type: ignore[arg-type]
    if identity_type == "agent":
        info = stored_identities_for_server(api_base)
        for handle in list(info.get("agent_keys", {})):
            delete_agent_key(api_base, handle)


def _forget_orcid_keys(
    api_base: str, orcid_bearer: StoredBearer | None, info: dict[str, Any]
) -> None:
    """Drop bound ORCID signing keys (RRP-0024). Uses the bearer's orcid_id
    (works under the keyring backend) plus any ids in the file-backend index."""
    if orcid_bearer is not None and orcid_bearer.identity is not None:
        delete_orcid_key(api_base, orcid_bearer.identity)
    for orcid_id in list(info.get("orcid_keys", {})):
        delete_orcid_key(api_base, orcid_id)


# ----------------------------- shared persistence helper -----------------------------


def _persist_bearer(
    api_base: str, bearer: Any, *, expires_in_seconds: int
) -> None:
    """``bearer`` is a :class:`rrxiv.client.BearerToken`; we wrap it in
    a :class:`StoredBearer` with timestamps."""
    stored = StoredBearer(
        api_base=api_base,
        identity_type=bearer.identity_type,
        identity=bearer.identity,
        token=bearer.token,
        issued_at_unix=_now(),
        expires_at_unix=_now() + expires_in_seconds,
    )
    store_bearer(stored)


__all__ = ["login_app"]
