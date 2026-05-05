"""Anonymous-with-attestation flow.

Anonymous users (browsers, drive-by readers) can mint a read-tier
bearer token by solving a CAPTCHA-style challenge. The token is
loosely bound to their IP and rate-limited at the anonymous tier.

The flow:

1. Client calls ``POST /auth/anonymous/challenge`` to get a challenge
   (currently a hCaptcha-style ``site_key`` + a server-issued
   ``challenge_id``).
2. Caller solves the challenge in whatever way the challenge type
   demands — for hCaptcha, that's running the widget in a browser and
   getting a ``response`` token.
3. Client calls ``POST /auth/anonymous/verify`` with
   ``challenge_id`` + the solution ``response``. Server validates and
   returns a bearer token.

For non-browser callers (CI scripts, server-to-server agents), the
*agent enrollment* flow is the right path — anonymous tokens are
specifically scoped to "I am a human-ish entity reading the corpus".
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from rrxiv.client.auth import BearerToken
from rrxiv.client.errors import raise_for_status


@dataclass(frozen=True, slots=True)
class AnonymousChallenge:
    """A challenge issued by the server."""

    challenge_id: str
    """Opaque ID; pass it back unchanged in the verify step."""

    challenge_type: str
    """The challenge mechanism. v0.1 supports ``"hcaptcha"`` only."""

    site_key: str
    """Per-server widget key. For hCaptcha this is what the JS widget
    needs to render the puzzle."""

    expires_in_seconds: int
    """Solve before this elapses or you'll need a fresh challenge."""


def request_anonymous_challenge(
    *,
    api_base: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> AnonymousChallenge:
    """Request a fresh challenge."""
    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(f"{base}/auth/anonymous/challenge")
    raise_for_status(resp)
    data = resp.json()
    return AnonymousChallenge(
        challenge_id=data["challenge_id"],
        challenge_type=data["challenge_type"],
        site_key=data["site_key"],
        expires_in_seconds=int(data["expires_in_seconds"]),
    )


@dataclass(frozen=True, slots=True)
class AnonymousChallengeResponse:
    """The solved challenge."""

    challenge_id: str
    response: str
    """The challenge mechanism's solution token. For hCaptcha, the
    ``h-captcha-response`` value the widget produces."""


def verify_anonymous_challenge(
    *,
    api_base: str,
    response: AnonymousChallengeResponse,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> BearerToken:
    """Submit a solved challenge and return the issued bearer token.

    Raises:
        rrxiv.client.errors.UnauthorizedError: solution rejected.
        rrxiv.client.errors.ValidationError: malformed request.
    """
    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(
            f"{base}/auth/anonymous/verify",
            json={
                "challenge_id": response.challenge_id,
                "response": response.response,
            },
        )
    raise_for_status(resp)
    data = resp.json()
    return BearerToken(
        token=data["token"],
        identity_type="anonymous",
        identity=None,
    )
