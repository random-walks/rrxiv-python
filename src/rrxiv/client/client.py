"""Synchronous HTTP client for rrxiv.

The protocol's HTTP API is sketched in ``rrxiv/schema/api.openapi.yaml``;
this client is the reference Python implementation that targets it.

v0.1 limitations:

- **Sync only.** An async variant on httpx.AsyncClient is a v0.2 addition.
- **No automatic retries.** The client surfaces 429s as
  :class:`RateLimitedError`; callers decide whether to retry. A future
  RRP could specify retry semantics for typical agents.
- **No live server to test against.** Tests use httpx's MockTransport
  to drive deterministic request/response pairs.

Usage::

    from rrxiv.client import RrxivClient
    from rrxiv.client.auth import BearerToken

    client = RrxivClient("https://rrxiv.com/api/v0")
    paper = client.get_paper("01923f8e-...")

    # Authenticated:
    auth = BearerToken("token-from-orcid-flow", "orcid", "0000-0001-...")
    client = RrxivClient("https://rrxiv.com/api/v0", auth=auth)
    new_annotation = client.create_annotation({...})
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import httpx

from rrxiv.client.auth import BearerToken, header
from rrxiv.client.errors import (
    raise_for_status,
)
from rrxiv.client.retry import (
    DEFAULT_RETRY_POLICY,
    RetryBudget,
    RetryPolicy,
    compute_wait,
    is_retryable_status,
    parse_retry_after,
    sleep_for,
)
from rrxiv.models import (
    CIR,
    Annotation,
    Claim,
    Paper,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0)


class RrxivClient:
    """Sync rrxiv HTTP API client.

    Wraps an ``httpx.Client``; close it via :py:meth:`close` or by using
    the client as a context manager.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: BearerToken | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str = "rrxiv-python/0.1",
        retry_policy: RetryPolicy | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.retry_policy = retry_policy if retry_policy is not None else DEFAULT_RETRY_POLICY
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        headers.update(header(auth))
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # Resource lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RrxivClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Endpoint methods
    # ------------------------------------------------------------------

    # ---- Papers ----

    def list_papers(
        self,
        *,
        author: str | None = None,
        topic: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``GET /papers`` — paginated list."""
        params = _drop_nulls(
            {"author": author, "topic": topic, "since": since, "cursor": cursor, "limit": limit}
        )
        return cast(dict[str, Any], self._json("GET", "/papers", params=params))

    def get_paper(self, paper_id: str) -> Paper:
        """``GET /papers/{id}`` — paper metadata."""
        data = self._json("GET", f"/papers/{paper_id}")
        return Paper.model_validate(data)

    def get_cir(self, paper_id: str) -> CIR:
        """``GET /papers/{id}/cir`` — full Canonical Intermediate Representation."""
        data = self._json("GET", f"/papers/{paper_id}/cir")
        return CIR.model_validate(data)

    # ---- Claims ----

    def list_claims(
        self,
        *,
        claim_type: str | None = None,
        evidence_type: str | None = None,
        replication_status: str | None = None,
        paper: str | None = None,
        canonical: bool | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``GET /claims``."""
        params = _drop_nulls(
            {
                "claim_type": claim_type,
                "evidence_type": evidence_type,
                "replication_status": replication_status,
                "paper": paper,
                "canonical": canonical,
                "cursor": cursor,
                "limit": limit,
            }
        )
        return cast(dict[str, Any], self._json("GET", "/claims", params=params))

    def get_claim(self, claim_id: str) -> Claim:
        """``GET /claims/{id}``."""
        data = self._json("GET", f"/claims/{claim_id}")
        return Claim.model_validate(data)

    def claim_depends_on(self, claim_id: str, *, depth: int = 1) -> dict[str, Any]:
        """``GET /claims/{id}/depends-on``."""
        return cast(
            dict[str, Any],
            self._json("GET", f"/claims/{claim_id}/depends-on", params={"depth": depth}),
        )

    def claim_dependents(self, claim_id: str, *, depth: int = 1) -> dict[str, Any]:
        """``GET /claims/{id}/dependents``."""
        return cast(
            dict[str, Any],
            self._json("GET", f"/claims/{claim_id}/dependents", params={"depth": depth}),
        )

    # ---- Annotations ----

    def list_annotations(
        self,
        *,
        target: str | None = None,
        annotation_type: str | None = None,
        created_by: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = _drop_nulls(
            {
                "target": target,
                "annotation_type": annotation_type,
                "created_by": created_by,
                "since": since,
                "cursor": cursor,
                "limit": limit,
            }
        )
        return cast(dict[str, Any], self._json("GET", "/annotations", params=params))

    def get_annotation(self, annotation_id: str) -> Annotation:
        data = self._json("GET", f"/annotations/{annotation_id}")
        return Annotation.model_validate(data)

    def create_annotation(
        self,
        annotation: dict[str, Any] | Annotation,
        *,
        idempotency_key: str | None = None,
    ) -> Annotation:
        """``POST /annotations`` — requires auth.

        Pass either a dict (will be validated against the schema before
        sending) or a pre-validated :class:`rrxiv.models.Annotation`.
        """
        if not isinstance(annotation, Annotation):
            annotation = Annotation.model_validate(annotation)
        body = annotation.model_dump(mode="json", exclude_none=True)
        idempotency_key = idempotency_key or _gen_idempotency_key()
        data = self._json(
            "POST",
            "/annotations",
            json=body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return Annotation.model_validate(data)

    # ---- Snapshots ----

    def latest_snapshot(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._json("GET", "/snapshots/latest"))

    # ---- Search ----

    def search_papers(
        self, q: str, *, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._json(
                "GET",
                "/search/papers",
                params=_drop_nulls({"q": q, "cursor": cursor, "limit": limit}),
            ),
        )

    def search_claims(
        self, q: str, *, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._json(
                "GET",
                "/search/claims",
                params=_drop_nulls({"q": q, "cursor": cursor, "limit": limit}),
            ),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        budget = RetryBudget(self.retry_policy)
        while True:
            response = self._http.request(
                method,
                path,
                params=params,
                json=json,
                headers=extra_headers,
            )
            if response.is_success:
                if response.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    return response.json()
                return None

            if not is_retryable_status(response.status_code, self.retry_policy):
                raise_for_status(response)

            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            wait = compute_wait(
                attempt=budget.attempts + 1,
                policy=self.retry_policy,
                retry_after_seconds=retry_after,
            )
            if not budget.can_retry(wait):
                raise_for_status(response)

            sleep_for(wait)
            budget.spend(wait)
            # loop and retry


def _drop_nulls(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _gen_idempotency_key() -> str:
    return f"rrxiv-py-{uuid.uuid4()}"
