"""HTTP client for the rrxiv protocol.

Public surface:

>>> from rrxiv.client import RrxivClient
>>> from rrxiv.client.auth import BearerToken
>>> from rrxiv.client.errors import RrxivError, NotFoundError, RateLimitedError, ...

The client targets the API sketched in
``rrxiv/schema/api.openapi.yaml``. v0.1 is sync-only; an async
variant lands in v0.2.
"""

from rrxiv.client.auth import BearerToken
from rrxiv.client.client import RrxivClient
from rrxiv.client.errors import (
    BadRequestError,
    ForbiddenError,
    IdempotencyKeyConflictError,
    NotFoundError,
    RateLimitedError,
    RrxivError,
    ServerError,
    UnauthorizedError,
    ValidationError,
)
from rrxiv.client.retry import (
    DEFAULT_RETRY_POLICY,
    NO_RETRY_POLICY,
    RetryPolicy,
)

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "NO_RETRY_POLICY",
    "BadRequestError",
    "BearerToken",
    "ForbiddenError",
    "IdempotencyKeyConflictError",
    "NotFoundError",
    "RateLimitedError",
    "RetryPolicy",
    "RrxivClient",
    "RrxivError",
    "ServerError",
    "UnauthorizedError",
    "ValidationError",
]
