"""Asynchronous HTTP client for rrxiv.

Mirror of :class:`rrxiv.client.client.RrxivClient` built on
``httpx.AsyncClient``. Useful when an agent or batch script wants to
fan out many reads concurrently — the read endpoints have no
side effects, and the protocol's locked rate-limit floors mean modest
concurrency stays comfortably under the limits.

Method surface mirrors the sync client exactly. The retry policy is
shared (same :class:`rrxiv.client.RetryPolicy`); under the hood we
``await asyncio.sleep`` instead of ``time.sleep``.

Tests use httpx's ``MockTransport`` and pytest-asyncio (already a
dev dep).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from rrxiv.client.auth import BearerToken, header
from rrxiv.client.client import (
    DEFAULT_TIMEOUT,
    _drop_nulls,
    _gen_idempotency_key,
)
from rrxiv.client.errors import raise_for_status
from rrxiv.client.retry import (
    DEFAULT_RETRY_POLICY,
    RetryBudget,
    RetryPolicy,
    compute_wait,
    is_retryable_status,
    parse_retry_after,
)
from rrxiv.client.signatures import AgentSigningAuth, AgentSigningKey
from rrxiv.models import (
    CIR,
    Annotation,
    Claim,
    Paper,
)


async def _async_sleep_for(seconds: float) -> None:  # pragma: no cover
    """Async sleep wrapper, monkey-patchable in tests."""
    if seconds > 0:
        await asyncio.sleep(seconds)


class AsyncRrxivClient:
    """Async rrxiv HTTP API client.

    Mirror of the sync :class:`rrxiv.client.RrxivClient`. Use
    ``async with AsyncRrxivClient(...)`` for the lifecycle, or call
    :py:meth:`aclose` explicitly.

    Methods are awaitable; they return the same pydantic models /
    dicts as the sync client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: BearerToken | None = None,
        agent_signing_key: AgentSigningKey | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "rrxiv-python/0.1 (async)",
        retry_policy: RetryPolicy | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.agent_signing_key = agent_signing_key
        self.retry_policy = retry_policy if retry_policy is not None else DEFAULT_RETRY_POLICY
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        headers.update(header(auth))
        self._signing_auth: AgentSigningAuth | None = (
            AgentSigningAuth(agent_signing_key) if agent_signing_key else None
        )
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncRrxivClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ---- Papers ----

    async def list_papers(
        self,
        *,
        author: str | None = None,
        topic: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = _drop_nulls(
            {"author": author, "topic": topic, "since": since, "cursor": cursor, "limit": limit}
        )
        return cast(dict[str, Any], await self._json("GET", "/papers", params=params))

    async def get_paper(self, paper_id: str) -> Paper:
        data = await self._json("GET", f"/papers/{paper_id}")
        return Paper.model_validate(data)

    async def get_cir(self, paper_id: str) -> CIR:
        data = await self._json("GET", f"/papers/{paper_id}/cir")
        return CIR.model_validate(data)

    # ---- Claims ----

    async def list_claims(
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
        return cast(dict[str, Any], await self._json("GET", "/claims", params=params))

    async def get_claim(self, claim_id: str) -> Claim:
        data = await self._json("GET", f"/claims/{claim_id}")
        return Claim.model_validate(data)

    async def claim_depends_on(self, claim_id: str, *, depth: int = 1) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._json(
                "GET", f"/claims/{claim_id}/depends-on", params={"depth": depth}
            ),
        )

    async def claim_dependents(self, claim_id: str, *, depth: int = 1) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._json(
                "GET", f"/claims/{claim_id}/dependents", params={"depth": depth}
            ),
        )

    # ---- Annotations ----

    async def list_annotations(
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
        return cast(dict[str, Any], await self._json("GET", "/annotations", params=params))

    async def get_annotation(self, annotation_id: str) -> Annotation:
        data = await self._json("GET", f"/annotations/{annotation_id}")
        return Annotation.model_validate(data)

    async def create_annotation(
        self,
        annotation: dict[str, Any] | Annotation,
        *,
        idempotency_key: str | None = None,
    ) -> Annotation:
        if not isinstance(annotation, Annotation):
            annotation = Annotation.model_validate(annotation)
        body = annotation.model_dump(mode="json", exclude_none=True)
        idempotency_key = idempotency_key or _gen_idempotency_key()
        data = await self._json(
            "POST",
            "/annotations",
            json=body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return Annotation.model_validate(data)

    # ---- Snapshots ----

    async def latest_snapshot(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._json("GET", "/snapshots/latest"))

    # ---- Search ----

    async def search_papers(
        self, q: str, *, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._json(
                "GET",
                "/search/papers",
                params=_drop_nulls({"q": q, "cursor": cursor, "limit": limit}),
            ),
        )

    async def search_claims(
        self, q: str, *, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._json(
                "GET",
                "/search/claims",
                params=_drop_nulls({"q": q, "cursor": cursor, "limit": limit}),
            ),
        )

    # ---- High-level helpers ----

    async def iter_papers(
        self,
        **filters: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator over all pages of /papers.

        Wraps :py:meth:`list_papers` with cursor-following so callers can
        ``async for paper in client.iter_papers(topic='x'):``.
        """
        cursor: str | None = filters.pop("cursor", None)
        while True:
            page = await self.list_papers(cursor=cursor, **filters)
            for item in page.get("items", []):
                yield item
            cursor = page.get("next_cursor")
            if not cursor:
                return

    async def iter_claims(
        self,
        **filters: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = filters.pop("cursor", None)
        while True:
            page = await self.list_claims(cursor=cursor, **filters)
            for item in page.get("items", []):
                yield item
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ---- Internal ----

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        budget = RetryBudget(self.retry_policy)
        sign = self._signing_auth if method.upper() not in ("GET", "HEAD") else None
        while True:
            response = await self._http.request(
                method,
                path,
                params=params,
                json=json,
                headers=extra_headers,
                auth=sign,
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

            await _async_sleep_for(wait)
            budget.spend(wait)


__all__ = ["AsyncRrxivClient"]
