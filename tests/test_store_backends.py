"""Parameterised tests across both Store backends (RRP-0011).

Anything ``MemoryStore`` does, ``SqliteStore`` must do too. The
parameterisation is the simplest "conformance suite" for store impls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rrxiv.server.store import (
    MemoryStore,
    SqliteStore,
    Store,
    store_from_url,
)
from rrxiv.server.store.protocol import (
    AgentIdentity,
    AgentRecord,
    AnonymousChallengeRecord,
    IdempotencyEntry,
    OrcidIdentity,
    PasteCodeEntry,
    TokenRecord,
)


def _make_memory() -> Store:
    return MemoryStore()


def _make_sqlite() -> Store:
    return SqliteStore(":memory:")


@pytest.fixture(params=[_make_memory, _make_sqlite], ids=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Store:
    s = request.param()
    yield s
    if hasattr(s, "close"):
        s.close()


# ----- Tokens -----


def test_token_round_trip(store: Store) -> None:
    record = TokenRecord(
        token="t1",
        identity=OrcidIdentity(orcid_id="0000-0001-2345-6789"),
        issued_at_unix=1700000000,
        expires_at_unix=1700003600,
    )
    store.add_token(record)
    loaded = store.get_token("t1")
    assert loaded == record


def test_token_revoke(store: Store) -> None:
    store.add_token(
        TokenRecord(
            token="t2",
            identity=AgentIdentity(handle="@bot"),
            issued_at_unix=1,
            expires_at_unix=2,
        )
    )
    store.revoke_token("t2")
    assert store.get_token("t2") is None


# ----- Agents -----


def test_agent_round_trip(store: Store) -> None:
    record = AgentRecord(
        handle="@my-bot",
        public_key_b64="abcd",
        contact="ops@x.com",
        enrolled_at_unix=1,
    )
    store.add_agent(record)
    assert store.get_agent("@my-bot") == record


# ----- Papers / claims / annotations -----


def test_paper_round_trip(store: Store) -> None:
    p = {"id": "p1", "title": "T"}
    store.add_paper(p)
    assert store.get_paper("p1") == p
    assert any(x["id"] == "p1" for x in store.list_papers())


def test_claim_round_trip(store: Store) -> None:
    c = {"id": "c1", "statement": "X."}
    store.add_claim(c)
    assert store.get_claim("c1") == c


def test_annotation_round_trip(store: Store) -> None:
    a = {"id": "ann1", "content": "hi"}
    store.add_annotation(a)
    assert store.get_annotation("ann1") == a


# ----- Sources / blobs -----


def test_source_round_trip(store: Store) -> None:
    blob = b"binary source archive"
    uri = store.save_source("p1", blob)
    assert uri.endswith("/p1/source")
    assert store.load_source("p1") == blob


def test_clear_corpus_truncates_papers_and_blobs(store: Store) -> None:
    """``clear_corpus`` drops papers/CIRs/claims/annotations/sources/PDFs
    but leaves tokens, agents, and snapshot metadata intact (those are
    operational state, separate from the read-corpus)."""
    # Seed both corpus + operational state.
    store.add_paper({"id": "p1", "title": "x"})
    store.add_cir({"id": "p1", "claims": [], "annotations": []})
    store.add_claim({"id": "p1:c1", "paper_id": "p1", "statement": "x"})
    store.add_annotation(
        {
            "id": "ann-1",
            "target_id": "p1",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "x",
            "created_at": "2026-05-20T00:00:00Z",
            "created_by": {"identity_type": "anonymous", "identity": ""},
        }
    )
    store.save_source("p1", b"bytes")
    store.save_rendered_pdf("p1", b"%PDF")

    # Operational state — should survive.
    tok = TokenRecord(
        token="keep",
        identity=AgentIdentity(handle="@x"),
        issued_at_unix=1,
        expires_at_unix=2,
    )
    store.add_token(tok)

    store.clear_corpus()

    assert store.list_papers() == []
    assert store.list_claims() == []
    assert store.list_annotations() == []
    assert store.get_cir("p1") is None
    assert store.load_source("p1") is None
    assert store.load_rendered_pdf("p1") is None

    # But tokens are untouched.
    assert store.get_token("keep") == tok


def test_snapshot_blob_round_trip(store: Store) -> None:
    blob = b"snapshot tarball"
    uri = store.save_snapshot_blob("snap1", blob)
    assert uri.endswith("/snap1")
    assert store.load_snapshot_blob("snap1") == blob


# ----- Idempotency -----


def test_idempotency_round_trip(store: Store) -> None:
    entry = IdempotencyEntry(
        body_sha256="abc",
        response_status=201,
        response_body={"x": 1},
        created_at_unix=1,
    )
    store.add_idempotency("tok", "key", entry)
    assert store.get_idempotency("tok", "key") == entry


# ----- Snapshots -----


def test_latest_snapshot_round_trip(store: Store) -> None:
    assert store.latest_snapshot() is None
    store.set_latest_snapshot({"id": "s1", "counts": {"papers": 3}})
    out = store.latest_snapshot()
    assert out == {"id": "s1", "counts": {"papers": 3}}


# ----- Challenges + paste codes -----


def test_challenge_consume(store: Store) -> None:
    rec = AnonymousChallengeRecord(
        challenge_id="c-x",
        challenge_type="hcaptcha",
        site_key="x",
        issued_at_unix=1,
        expires_at_unix=2,
    )
    store.add_challenge(rec)
    assert store.get_challenge("c-x").consumed is False
    store.consume_challenge("c-x")
    assert store.get_challenge("c-x").consumed is True


def test_paste_code_consume(store: Store) -> None:
    rec = PasteCodeEntry(
        code="P-X",
        orcid_id="0000-0001",
        issued_at_unix=1,
        expires_at_unix=2,
    )
    store.add_paste_code(rec)
    assert store.get_paste_code("P-X").consumed is False
    store.consume_paste_code("P-X")
    assert store.get_paste_code("P-X").consumed is True


# ----- Rate limit -----


def test_rate_window_increments(store: Store) -> None:
    assert store.record_request("bucket", 1000) == 1
    assert store.record_request("bucket", 1001) == 2
    # Outside the 60-second window — pruned.
    assert store.record_request("bucket", 1100) == 1


# ----- Factory -----


def test_store_from_url_memory() -> None:
    s = store_from_url("memory://")
    assert isinstance(s, MemoryStore)


def test_store_from_url_sqlite_memory() -> None:
    s = store_from_url("sqlite:///:memory:")
    assert isinstance(s, SqliteStore)
    s.close()


def test_store_from_url_sqlite_file(tmp_path: Path) -> None:
    db_path = tmp_path / "rrxiv.db"
    s = store_from_url(f"sqlite:///{db_path}")
    s.add_paper({"id": "p1"})
    s.close()
    # Reopen — data persists.
    s2 = store_from_url(f"sqlite:///{db_path}")
    assert s2.get_paper("p1") == {"id": "p1"}
    s2.close()


def test_store_from_url_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown store_url"):
        store_from_url("postgres://x/y")
