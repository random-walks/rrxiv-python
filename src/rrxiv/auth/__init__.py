"""Token acquisition flows for the rrxiv API.

Three identity types per ``spec/0007-api.md`` §"Auth model":

- :mod:`rrxiv.auth.orcid` — OAuth 2.0 against orcid.org as the IdP.
- :mod:`rrxiv.auth.agent` — Ed25519 keypair enrollment for tools/bots.
- :mod:`rrxiv.auth.anonymous` — CAPTCHA-style challenge for read-tier
  bearer tokens.

Each flow ends in a :class:`rrxiv.client.BearerToken` that callers
attach to a :class:`rrxiv.client.RrxivClient` via ``auth=`` for
authenticated requests.

These are protocol-shape helpers — they construct the request payloads,
call the right endpoints, parse responses. They do *not* run a local
HTTP server for OAuth callbacks or solve CAPTCHA challenges; those
remain caller concerns. See each submodule for details.
"""

from __future__ import annotations

from rrxiv.auth.agent import (
    AgentEnrollmentRequest,
    AgentEnrollmentResponse,
    enroll_agent,
    sign_enrollment_payload,
)
from rrxiv.auth.anonymous import (
    AnonymousChallenge,
    AnonymousChallengeResponse,
    request_anonymous_challenge,
    verify_anonymous_challenge,
)
from rrxiv.auth.orcid import (
    OrcidAuthorizationUrl,
    OrcidTokenResponse,
    build_orcid_authorization_url,
    exchange_orcid_code,
)
from rrxiv.auth.refresh import RefreshedBearer, refresh_bearer

__all__ = [
    "AgentEnrollmentRequest",
    "AgentEnrollmentResponse",
    "AnonymousChallenge",
    "AnonymousChallengeResponse",
    "OrcidAuthorizationUrl",
    "OrcidTokenResponse",
    "RefreshedBearer",
    "build_orcid_authorization_url",
    "enroll_agent",
    "exchange_orcid_code",
    "refresh_bearer",
    "request_anonymous_challenge",
    "sign_enrollment_payload",
    "verify_anonymous_challenge",
]
