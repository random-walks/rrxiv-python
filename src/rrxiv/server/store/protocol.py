"""Storage Protocol for the reference server.

A ``Store`` exposes just the operations the routers / services need.
Concrete implementations (in-memory, SQLite, Postgres) implement this
without changing route code.
"""

from __future__ import annotations

from collections.abc import Iterable
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
class OrcidKeyRecord:
    """An Ed25519 signing key bound to an ORCID identity (RRP-0024).

    Soft-revocable: ``revoked_at_unix`` is set on revoke but the row
    stays so historical signatures remain verifiable for audit replay.
    Active set = ``revoked_at_unix is None``.
    """

    orcid_id: str
    key_id: str  # "key:<32-hex>" — server-minted, immutable
    public_key_b64: str
    label: str
    created_at_unix: int
    revoked_at_unix: int | None = None


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

    # ----- ORCID-bound signing keys (RRP-0024) -----
    def add_orcid_key(self, record: OrcidKeyRecord) -> None: ...
    """Persist a new ORCID-bound Ed25519 key. key_id is server-minted
    by the caller (``"key:" + secrets.token_hex(16)``). Idempotent on
    (orcid_id, public_key_b64): re-binding the same public key returns
    a freshly-stamped record with a new key_id (callers preferring
    deduplication should check ``list_orcid_keys`` first)."""

    def get_orcid_key(self, key_id: str) -> OrcidKeyRecord | None: ...
    """Look up a key by its ``key:<32-hex>`` id. Returns the record
    whether active or revoked — the signature middleware MUST check
    ``revoked_at_unix`` and reject revoked keys on write paths."""

    def list_orcid_keys(
        self, orcid_id: str, *, include_revoked: bool = False
    ) -> list[OrcidKeyRecord]: ...
    """All keys bound to ``orcid_id``. Defaults to active set only."""

    def revoke_orcid_key(self, key_id: str, *, now_unix: int) -> None: ...
    """Soft-revoke: set ``revoked_at_unix = now_unix``. No-op if the
    key is already revoked. The row stays in the store so historical
    signatures (annotations / submissions made before revocation)
    remain verifiable for audit replay."""

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

    # ----- Claim view counter (RRP-0023, Sprint 22) -----
    def bump_claim_view(self, claim_id: str) -> int: ...
    """Increment the view counter for a claim. Returns the new
    cumulative count. Idempotent at the per-request level —
    repeated calls increment, but a single GET /claims/{id} should
    call this exactly once. Implementations MAY swallow the call
    on a missing claim_id; callers shouldn't depend on a return
    value when the claim doesn't exist (return 0 is acceptable)."""

    def get_claim_views(self, claim_id: str) -> int: ...
    """Read the current view count for a claim, without bumping it.
    Returns 0 for unknown claim ids."""

    def list_claim_views(self) -> dict[str, int]: ...
    """Snapshot of every claim_id → view_count pair. Used by
    /stats/pulse to compute the top-viewed leaderboard without
    walking the full claims table N times."""

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

    # ----- Rendered artifacts (PDF / HTML) -----
    def save_rendered_pdf(self, paper_id: str, blob: bytes) -> str: ...
    """Persist a compiled PDF; return the URI clients can fetch via
    GET /papers/{id}/pdf."""
    def load_rendered_pdf(self, paper_id: str) -> bytes | None: ...

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

    # ----- Corpus management -----
    def clear_corpus(self) -> None: ...
    """Drop every paper, CIR, claim, annotation, source archive, and
    rendered PDF. Used by ``rrxiv seed-store --reset`` to wipe stale
    rows before re-seeding so a paper whose claim IDs changed prefix
    (e.g. parser meta-slug → canonical UUID) no longer leaves orphans.

    Does NOT touch tokens, agents, challenges, snapshots, or rate
    limiting state — those are operational/auth concerns separate
    from the read corpus.
    """

    def replace_seed_papers(self, paper_ids: Iterable[str]) -> None: ...
    """Delete ONLY the given papers and their derived rows — CIR, claims,
    source archive, rendered PDF, and those claims' view counters — so a
    reseed can re-insert them, while leaving every OTHER paper and ALL
    annotations intact.

    Used by ``rrxiv seed-store --preserve-community`` to refresh the
    canonical corpus on a live instance WITHOUT wiping externally
    submitted papers or any community annotations — unlike
    :meth:`clear_corpus`, which truncates everything. A paper's claims
    are matched by the owning paper's ``id_slug`` (RRP-0013 / RRP-0029),
    resolved from the stored paper record before deletion; annotations
    are never deleted, even those targeting a replaced seed paper.
    Unknown ids are ignored.
    """


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
    # RRP-0024 — ORCID-bound signing keys. Keyed by key_id.
    orcid_keys: dict[str, OrcidKeyRecord] = field(default_factory=dict)
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
    rendered_pdfs: dict[str, bytes] = field(default_factory=dict)
    snapshot_blobs: dict[str, bytes] = field(default_factory=dict)
    # Sprint 22 — per-claim view counter. Bumped by claims/router.get_claim;
    # surfaced via the `top_claims_by_views` leaderboard in /stats/pulse
    # and as `views_count` on the claim's read response.
    claim_views: dict[str, int] = field(default_factory=dict)
