"""In-memory ``Store`` implementation."""

from __future__ import annotations

from typing import Any

from rrxiv.server.store.protocol import (
    AgentRecord,
    AnonymousChallengeRecord,
    IdempotencyEntry,
    PasteCodeEntry,
    StoreState,
    TokenRecord,
)


class MemoryStore:
    """Process-local ``Store``. State lost on restart.

    Suitable for tests, local dev, and the ``rrxiv serve`` reference
    instance. Production deployments use a persistent backend.
    """

    def __init__(self) -> None:
        self.state = StoreState()

    # ----- Tokens -----
    def add_token(self, record: TokenRecord) -> None:
        self.state.tokens[record.token] = record

    def get_token(self, token: str) -> TokenRecord | None:
        return self.state.tokens.get(token)

    def revoke_token(self, token: str) -> None:
        self.state.tokens.pop(token, None)

    # ----- Agents -----
    def add_agent(self, record: AgentRecord) -> None:
        self.state.agents[record.handle] = record

    def get_agent(self, handle: str) -> AgentRecord | None:
        return self.state.agents.get(handle)

    # ----- Anonymous challenges -----
    def add_challenge(self, record: AnonymousChallengeRecord) -> None:
        self.state.challenges[record.challenge_id] = record

    def get_challenge(self, challenge_id: str) -> AnonymousChallengeRecord | None:
        return self.state.challenges.get(challenge_id)

    def consume_challenge(self, challenge_id: str) -> None:
        c = self.state.challenges.get(challenge_id)
        if c is not None:
            c.consumed = True

    # ----- ORCID paste codes -----
    def add_paste_code(self, entry: PasteCodeEntry) -> None:
        self.state.paste_codes[entry.code] = entry

    def get_paste_code(self, code: str) -> PasteCodeEntry | None:
        return self.state.paste_codes.get(code)

    def consume_paste_code(self, code: str) -> None:
        e = self.state.paste_codes.get(code)
        if e is not None:
            e.consumed = True

    # ----- Papers -----
    def add_paper(self, paper: dict[str, Any]) -> None:
        self.state.papers[paper["id"]] = dict(paper)

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        return self.state.papers.get(paper_id)

    def list_papers(self) -> list[dict[str, Any]]:
        return list(self.state.papers.values())

    def add_cir(self, cir: dict[str, Any]) -> None:
        self.state.cirs[cir["id"]] = dict(cir)

    def get_cir(self, paper_id: str) -> dict[str, Any] | None:
        return self.state.cirs.get(paper_id)

    # ----- Claims -----
    def add_claim(self, claim: dict[str, Any]) -> None:
        self.state.claims[claim["id"]] = dict(claim)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        return self.state.claims.get(claim_id)

    def list_claims(self) -> list[dict[str, Any]]:
        return list(self.state.claims.values())

    # ----- Annotations -----
    def add_annotation(self, ann: dict[str, Any]) -> None:
        self.state.annotations[ann["id"]] = dict(ann)

    def get_annotation(self, ann_id: str) -> dict[str, Any] | None:
        return self.state.annotations.get(ann_id)

    def list_annotations(self) -> list[dict[str, Any]]:
        return list(self.state.annotations.values())

    # ----- Idempotency -----
    def get_idempotency(self, token: str, key: str) -> IdempotencyEntry | None:
        return self.state.idempotency.get((token, key))

    def add_idempotency(
        self, token: str, key: str, entry: IdempotencyEntry
    ) -> None:
        self.state.idempotency[(token, key)] = entry

    # ----- Sources -----
    def save_source(self, paper_id: str, blob: bytes) -> str:
        self.state.sources[paper_id] = blob
        return f"/api/v0/papers/{paper_id}/source"

    def load_source(self, paper_id: str) -> bytes | None:
        return self.state.sources.get(paper_id)

    # ----- Rendered PDFs -----
    def save_rendered_pdf(self, paper_id: str, blob: bytes) -> str:
        self.state.rendered_pdfs[paper_id] = blob
        return f"/api/v0/papers/{paper_id}/pdf"

    def load_rendered_pdf(self, paper_id: str) -> bytes | None:
        return self.state.rendered_pdfs.get(paper_id)

    # ----- Snapshots -----
    def latest_snapshot(self) -> dict[str, Any] | None:
        return self.state.latest_snapshot

    def set_latest_snapshot(self, manifest: dict[str, Any]) -> None:
        self.state.latest_snapshot = dict(manifest)

    def save_snapshot_blob(self, snapshot_id: str, blob: bytes) -> str:
        self.state.snapshot_blobs[snapshot_id] = blob
        return f"/api/v0/snapshots/{snapshot_id}"

    def load_snapshot_blob(self, snapshot_id: str) -> bytes | None:
        return self.state.snapshot_blobs.get(snapshot_id)

    # ----- Rate limiting (sliding window, 60 seconds) -----
    def record_request(self, token_or_ip: str, now_unix: int) -> int:
        window = self.state.rate_window.setdefault(token_or_ip, [])
        cutoff = now_unix - 60
        # Drop expired entries.
        while window and window[0] < cutoff:
            window.pop(0)
        window.append(now_unix)
        return len(window)

    # ----- Corpus management -----
    def clear_corpus(self) -> None:
        self.state.papers.clear()
        self.state.cirs.clear()
        self.state.claims.clear()
        self.state.annotations.clear()
        self.state.sources.clear()
        self.state.rendered_pdfs.clear()
