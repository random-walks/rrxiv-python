"""Storage Protocol for the reference server.

A ``Store`` exposes just the operations the routers / services need.
Concrete implementations (in-memory, SQLite, Postgres) implement this
without changing route code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

IdentityType = Literal["orcid", "agent", "anonymous"]


@dataclass(frozen=True, slots=True)
class OrcidIdentity:
    orcid_id: str
    """The ORCID iD, e.g. 0000-0001-2345-6789."""


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    handle: str
    """Agent handle (with @)."""


@dataclass(frozen=True, slots=True)
class AnonymousIdentity:
    """Marker — anonymous tokens have no identity beyond their token."""

    challenge_id: str
    """The challenge that minted this identity (for audit)."""


Identity = OrcidIdentity | AgentIdentity | AnonymousIdentity


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """One issued bearer token."""

    token: str
    identity: Identity
    issued_at_unix: int
    expires_at_unix: int


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """An enrolled agent."""

    handle: str
    public_key_b64: str
    contact: str | None
    enrolled_at_unix: int


@dataclass(frozen=True, slots=True)
class IdempotencyEntry:
    """A cached write response per (token, idempotency_key)."""

    body_sha256: str
    response_status: int
    response_body: dict[str, Any]
    created_at_unix: int


@dataclass(slots=True)
class AnonymousChallengeRecord:
    challenge_id: str
    challenge_type: str
    site_key: str
    issued_at_unix: int
    expires_at_unix: int
    consumed: bool = False


@dataclass(slots=True)
class PasteCodeEntry:
    """An ORCID paste-back code (RRP-0006 fallback)."""

    code: str
    orcid_id: str
    issued_at_unix: int
    expires_at_unix: int
    consumed: bool = False


class Store(Protocol):
    """The shape every storage backend must implement."""

    # ----- Tokens -----
    def add_token(self, record: TokenRecord) -> None: ...
    def get_token(self, token: str) -> TokenRecord | None: ...
    def revoke_token(self, token: str) -> None: ...

    # ----- Agents -----
    def add_agent(self, record: AgentRecord) -> None: ...
    def get_agent(self, handle: str) -> AgentRecord | None: ...

    # ----- Anonymous challenges -----
    def add_challenge(self, record: AnonymousChallengeRecord) -> None: ...
    def get_challenge(self, challenge_id: str) -> AnonymousChallengeRecord | None: ...
    def consume_challenge(self, challenge_id: str) -> None: ...

    # ----- ORCID paste codes -----
    def add_paste_code(self, entry: PasteCodeEntry) -> None: ...
    def get_paste_code(self, code: str) -> PasteCodeEntry | None: ...
    def consume_paste_code(self, code: str) -> None: ...

    # ----- Papers / claims / annotations (in-memory dicts in v0.1) -----
    def add_paper(self, paper: dict[str, Any]) -> None: ...
    def get_paper(self, paper_id: str) -> dict[str, Any] | None: ...
    def list_papers(self) -> list[dict[str, Any]]: ...

    def add_cir(self, cir: dict[str, Any]) -> None: ...
    def get_cir(self, paper_id: str) -> dict[str, Any] | None: ...

    def add_claim(self, claim: dict[str, Any]) -> None: ...
    def get_claim(self, claim_id: str) -> dict[str, Any] | None: ...
    def list_claims(self) -> list[dict[str, Any]]: ...

    def add_annotation(self, ann: dict[str, Any]) -> None: ...
    def get_annotation(self, ann_id: str) -> dict[str, Any] | None: ...
    def list_annotations(self) -> list[dict[str, Any]]: ...

    # ----- Idempotency -----
    def get_idempotency(
        self, token: str, key: str
    ) -> IdempotencyEntry | None: ...
    def add_idempotency(
        self, token: str, key: str, entry: IdempotencyEntry
    ) -> None: ...

    # ----- Sources -----
    def save_source(self, paper_id: str, blob: bytes) -> str: ...
    """Persist the paper's source archive bytes; return the
    server-relative URI clients can fetch via /papers/{id}/source."""
    def load_source(self, paper_id: str) -> bytes | None: ...

    # ----- Snapshots -----
    def latest_snapshot(self) -> dict[str, Any] | None: ...
    def set_latest_snapshot(self, manifest: dict[str, Any]) -> None: ...
    def save_snapshot_blob(self, snapshot_id: str, blob: bytes) -> str: ...
    """Persist a snapshot tarball; return the URI."""
    def load_snapshot_blob(self, snapshot_id: str) -> bytes | None: ...

    # ----- Rate limiting (in-memory; per-process) -----
    def record_request(self, token_or_ip: str, now_unix: int) -> int: ...
    """Record a request and return the count in the current 60-second
    sliding window."""


@dataclass(slots=True)
class StoreState:
    """Plain dataclass holding all the in-memory state. Useful as the
    backing for :class:`MemoryStore` and as a ``state`` snapshot for
    tests that want to introspect.

    Kept separate so test code can poke at fields without going
    through the protocol surface.
    """

    tokens: dict[str, TokenRecord] = field(default_factory=dict)
    agents: dict[str, AgentRecord] = field(default_factory=dict)
    challenges: dict[str, AnonymousChallengeRecord] = field(default_factory=dict)
    paste_codes: dict[str, PasteCodeEntry] = field(default_factory=dict)
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    cirs: dict[str, dict[str, Any]] = field(default_factory=dict)
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_snapshot: dict[str, Any] | None = None
    idempotency: dict[tuple[str, str], IdempotencyEntry] = field(default_factory=dict)
    rate_window: dict[str, list[int]] = field(default_factory=dict)
    sources: dict[str, bytes] = field(default_factory=dict)
    snapshot_blobs: dict[str, bytes] = field(default_factory=dict)
