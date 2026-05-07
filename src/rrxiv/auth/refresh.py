"""Bearer-token refresh flow (RRP-0009)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from rrxiv.client.auth import BearerToken
from rrxiv.client.errors import raise_for_status


@dataclass(frozen=True, slots=True)
class RefreshedBearer:
    """Result of a successful refresh."""

    token: BearerToken
    expires_in_seconds: int


def refresh_bearer(
    *,
    api_base: str,
    current: BearerToken,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> RefreshedBearer:
    """Exchange ``current`` for a fresh bearer (RRP-0009).

    The new bearer carries the same identity_type and identity as the
    old one. The server revokes the old token at the moment of
    issuance.

    Raises:
        rrxiv.client.errors.UnauthorizedError: the current token is
            unknown, expired, or has been revoked.
        rrxiv.client.errors.ForbiddenError: anonymous tokens cannot
            be refreshed.
    """
    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(
            f"{base}/auth/refresh",
            headers={"Authorization": f"Bearer {current.token}"},
        )
    raise_for_status(resp)
    body = resp.json()
    return RefreshedBearer(
        token=BearerToken(
            token=body["token"],
            identity_type=current.identity_type,
            identity=current.identity,
        ),
        expires_in_seconds=int(body["expires_in_seconds"]),
    )
