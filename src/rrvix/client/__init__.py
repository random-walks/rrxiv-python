"""HTTP client for the rrvix protocol.

Public surface:

>>> from rrvix.client import RrvixClient
>>> from rrvix.client.auth import BearerToken
>>> from rrvix.client.errors import RrvixError, NotFoundError, RateLimitedError, ...

The client targets the API sketched in
``rrvix/schema/api.openapi.yaml``. v0.1 is sync-only; an async
variant lands in v0.2.
"""

from rrvix.client.auth import BearerToken
from rrvix.client.client import RrvixClient
from rrvix.client.errors import (
    BadRequestError,
    ForbiddenError,
    IdempotencyKeyConflictError,
    NotFoundError,
    RateLimitedError,
    RrvixError,
    ServerError,
    UnauthorizedError,
    ValidationError,
)

__all__ = [
    "BadRequestError",
    "BearerToken",
    "ForbiddenError",
    "IdempotencyKeyConflictError",
    "NotFoundError",
    "RateLimitedError",
    "RrvixClient",
    "RrvixError",
    "ServerError",
    "UnauthorizedError",
    "ValidationError",
]
