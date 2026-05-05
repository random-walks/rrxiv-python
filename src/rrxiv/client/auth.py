"""Authentication helpers for the rrxiv HTTP client.

v0.1 supports bearer-token auth covering all three identity types
(ORCID, agent handle, anonymous-with-attestation). The actual
identity-establishing handshakes (OAuth, agent enrollment, anonymous
attestation) happen out-of-band; the client is given a token and
attaches it as ``Authorization: Bearer <token>``.

Future v0.2 work: integrate the token-acquisition flows directly so
callers don't have to handle the handshake elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

IdentityType = Literal["orcid", "agent", "anonymous"]


@dataclass(frozen=True, slots=True)
class BearerToken:
    """An opaque bearer token plus the identity type it represents.

    The identity type is metadata only — it doesn't change how the
    token is sent. It's recorded so the caller can introspect which
    identity is being used for a given client.
    """

    token: str
    identity_type: IdentityType
    identity: str | None = None
    """Optional human-readable identity (ORCID iD, agent handle, etc.)."""


def header(token: BearerToken | None) -> dict[str, str]:
    """Render the auth header dict, or empty if no token."""
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token.token}"}
