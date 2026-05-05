"""ORCID OAuth 2.0 flow.

The rrxiv server is the OAuth client; orcid.org is the IdP. The user
points their browser at the authorization URL, approves the scope, and
orcid.org redirects to the rrxiv server with a code that the server
exchanges for an ORCID iD + access token. The server then mints its
own bearer token tied to that ORCID iD and returns it to the caller.

This module provides:

- :func:`build_orcid_authorization_url` — construct the URL to open in
  the user's browser. Pure function; no network.
- :func:`exchange_orcid_code` — given the authorization code that
  orcid.org redirected to your ``redirect_uri``, exchange it at the
  rrxiv server's ``/auth/orcid/callback`` endpoint for a
  :class:`rrxiv.client.BearerToken`.

The OAuth dance itself (running a local listener for the redirect, or
having the user paste the code back) is the caller's responsibility.
The most common shapes are covered in
``spec/0007-api.md`` §"Auth model".
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from rrxiv.client.auth import BearerToken
from rrxiv.client.errors import raise_for_status


@dataclass(frozen=True, slots=True)
class OrcidAuthorizationUrl:
    """The URL to open in the user's browser plus the state value.

    The caller must verify that the ``state`` returned by orcid.org
    matches ``state`` here — otherwise the authorization code may be
    cross-site forgery.
    """

    url: str
    state: str


def build_orcid_authorization_url(
    *,
    api_base: str,
    redirect_uri: str,
    scope: str = "/authenticate",
    state: str | None = None,
) -> OrcidAuthorizationUrl:
    """Construct the URL the user opens in their browser.

    The rrxiv server proxies to orcid.org, so this URL targets
    ``{api_base}/auth/orcid/start`` rather than orcid.org directly.
    The server handles client-id rotation, scope validation, etc.

    Args:
        api_base: rrxiv API base URL, e.g. ``https://rrxiv.com/api/v0``.
            Trailing slash optional.
        redirect_uri: where orcid.org should redirect after auth. Must
            be one the rrxiv server has registered with orcid.org —
            typically a ``localhost:<port>`` your CLI is listening on.
        scope: ORCID OAuth scope. ``/authenticate`` is the minimum
            (just establishes identity).
        state: CSRF token. If ``None``, one is generated.
    """
    state_val = state if state is not None else secrets.token_urlsafe(24)
    base = api_base.rstrip("/")
    qs = urlencode(
        {"redirect_uri": redirect_uri, "scope": scope, "state": state_val}
    )
    return OrcidAuthorizationUrl(url=f"{base}/auth/orcid/start?{qs}", state=state_val)


@dataclass(frozen=True, slots=True)
class OrcidTokenResponse:
    """What ``/auth/orcid/callback`` returns on success."""

    token: str
    orcid_id: str
    expires_in_seconds: int | None


def exchange_orcid_code(
    *,
    api_base: str,
    code: str,
    state: str,
    expected_state: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> BearerToken:
    """Exchange an OAuth authorization code for a rrxiv bearer token.

    Args:
        api_base: rrxiv API base URL.
        code: the ``code`` query param orcid.org redirected with.
        state: the ``state`` query param orcid.org redirected with.
        expected_state: the ``state`` value from
            :class:`OrcidAuthorizationUrl`. Mismatch raises
            :class:`ValueError`.
        transport: optional ``httpx.BaseTransport`` for tests.
        timeout: request timeout, seconds.

    Raises:
        ValueError: state mismatch (CSRF protection).
        rrxiv.client.errors.UnauthorizedError: code rejected by ORCID
            or the rrxiv server.
    """
    if state != expected_state:
        raise ValueError("OAuth state mismatch — possible CSRF; rejecting code")

    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(
            f"{base}/auth/orcid/callback",
            json={"code": code, "state": state},
        )
    raise_for_status(resp)
    body = resp.json()
    return BearerToken(
        token=body["token"],
        identity_type="orcid",
        identity=body["orcid_id"],
    )
