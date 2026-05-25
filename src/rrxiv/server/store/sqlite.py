"""SQLite persistent store (RRP-0011).

A `Store`-protocol implementation backed by stdlib `sqlite3`. Schema
is JSON-blob-per-row for the dataclass fields plus typed columns for
the bits we query on.

Configuration: ``RRXIV_STORE_URL=sqlite:///path/to/db.sqlite`` (or
``sqlite:///:memory:`` for tests).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from typing import Any

from rrxiv.server.store.protocol import (
    AgentIdentity,
    AgentRecord,
    AnonymousChallengeRecord,
    AnonymousIdentity,
    IdempotencyEntry,
    Identity,
    OrcidIdentity,
    PasteCodeEntry,
    TokenRecord,
)

SCHEMA_VERSION = 1

_INIT_SQL = """\
CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  identity_type TEXT NOT NULL,
  identity_payload TEXT NOT NULL,
  issued_at_unix INTEGER NOT NULL,
  expires_at_unix INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON tokens(expires_at_unix);

CREATE TABLE IF NOT EXISTS agents (
  handle TEXT PRIMARY KEY,
  public_key_b64 TEXT NOT NULL,
  contact TEXT,
  enrolled_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cirs   (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotations (id TEXT PRIMARY KEY, payload TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (paper_id TEXT PRIMARY KEY, blob BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS rendered_pdfs (paper_id TEXT PRIMARY KEY, blob BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot_blobs (snapshot_id TEXT PRIMARY KEY, blob BLOB NOT NULL);

CREATE TABLE IF NOT EXISTS challenges (
  challenge_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paste_codes (
  code TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
  token TEXT NOT NULL,
  key TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  response_status INTEGER NOT NULL,
  response_body TEXT NOT NULL,
  created_at_unix INTEGER NOT NULL,
  PRIMARY KEY (token, key)
);

CREATE TABLE IF NOT EXISTS rate_window (
  bucket TEXT PRIMARY KEY,
  timestamps TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Sprint 22: per-claim view counter. Bumped by claims/router.get_claim
-- on every successful read; surfaced via /stats/pulse leaderboards and
-- as the `views_count` field on the claim's read response.
-- One row per claim, even if the claim later disappears (we keep the
-- count so the leaderboard can reference the title from a snapshot).
CREATE TABLE IF NOT EXISTS claim_views (
  claim_id TEXT PRIMARY KEY,
  count    INTEGER NOT NULL DEFAULT 0
);
"""


def parse_store_url(url: str) -> str | None:
    """Extract the SQLite path from a ``sqlite:///...`` URL.

    Returns ``None`` if the URL is not a sqlite URL.
    Returns ``":memory:"`` for ``sqlite:///:memory:``.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix) :]
    if rest == ":memory:":
        return ":memory:"
    # Convert relative-from-cwd paths like "./rrxiv.db" to absolute.
    return rest


class SqliteStore:
    """Persistent ``Store`` backed by a single SQLite database.

    v0.1: single connection per process, serialised by the GIL for
    sync handlers; FastAPI's threadpool is fine because SQLite
    handles its own locking. Multi-process deployments need a
    different backend (Postgres future RRP).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        # check_same_thread=False because FastAPI may invoke us from
        # the threadpool. We serialise via self._lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_INIT_SQL)
        self._stamp_schema_version()
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _stamp_schema_version(self) -> None:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        cur.close()

    # ----- Tokens -----
    def add_token(self, record: TokenRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tokens(token, identity_type, "
                "identity_payload, issued_at_unix, expires_at_unix) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.token,
                    _identity_type_str(record.identity),
                    json.dumps(_identity_to_dict(record.identity)),
                    record.issued_at_unix,
                    record.expires_at_unix,
                ),
            )
            self._conn.commit()

    def get_token(self, token: str) -> TokenRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT token, identity_type, identity_payload, "
                "issued_at_unix, expires_at_unix FROM tokens WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        return TokenRecord(
            token=row[0],
            identity=_identity_from_dict(row[1], json.loads(row[2])),
            issued_at_unix=int(row[3]),
            expires_at_unix=int(row[4]),
        )

    def revoke_token(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
            self._conn.commit()

    # ----- Agents -----
    def add_agent(self, record: AgentRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agents(handle, public_key_b64, contact, "
                "enrolled_at_unix) VALUES (?, ?, ?, ?)",
                (
                    record.handle,
                    record.public_key_b64,
                    record.contact,
                    record.enrolled_at_unix,
                ),
            )
            self._conn.commit()

    def get_agent(self, handle: str) -> AgentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT handle, public_key_b64, contact, enrolled_at_unix "
                "FROM agents WHERE handle = ?",
                (handle,),
            ).fetchone()
        if row is None:
            return None
        return AgentRecord(
            handle=row[0],
            public_key_b64=row[1],
            contact=row[2],
            enrolled_at_unix=int(row[3]),
        )

    # ----- Anonymous challenges -----
    def add_challenge(self, record: AnonymousChallengeRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO challenges(challenge_id, payload, "
                "expires_at_unix) VALUES (?, ?, ?)",
                (
                    record.challenge_id,
                    json.dumps(asdict(record)),
                    record.expires_at_unix,
                ),
            )
            self._conn.commit()

    def get_challenge(self, challenge_id: str) -> AnonymousChallengeRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        if row is None:
            return None
        return AnonymousChallengeRecord(**json.loads(row[0]))

    def consume_challenge(self, challenge_id: str) -> None:
        existing = self.get_challenge(challenge_id)
        if existing is None:
            return
        existing.consumed = True
        self.add_challenge(existing)

    # ----- Paste codes -----
    def add_paste_code(self, entry: PasteCodeEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO paste_codes(code, payload, "
                "expires_at_unix) VALUES (?, ?, ?)",
                (entry.code, json.dumps(asdict(entry)), entry.expires_at_unix),
            )
            self._conn.commit()

    def get_paste_code(self, code: str) -> PasteCodeEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM paste_codes WHERE code = ?", (code,)
            ).fetchone()
        if row is None:
            return None
        return PasteCodeEntry(**json.loads(row[0]))

    def consume_paste_code(self, code: str) -> None:
        existing = self.get_paste_code(code)
        if existing is None:
            return
        existing.consumed = True
        self.add_paste_code(existing)

    # ----- Papers / claims / annotations -----
    def _add_blob(self, table: str, key: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table}(id, payload) VALUES (?, ?)",
                (key, json.dumps(payload)),
            )
            self._conn.commit()

    def _get_blob(self, table: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])  # type: ignore[no-any-return]

    def _list_blobs(self, table: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM {table}"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def add_paper(self, paper: dict[str, Any]) -> None:
        self._add_blob("papers", paper["id"], paper)

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        return self._get_blob("papers", paper_id)

    def list_papers(self) -> list[dict[str, Any]]:
        return self._list_blobs("papers")

    def add_cir(self, cir: dict[str, Any]) -> None:
        self._add_blob("cirs", cir["id"], cir)

    def get_cir(self, paper_id: str) -> dict[str, Any] | None:
        return self._get_blob("cirs", paper_id)

    def add_claim(self, claim: dict[str, Any]) -> None:
        self._add_blob("claims", claim["id"], claim)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        return self._get_blob("claims", claim_id)

    def list_claims(self) -> list[dict[str, Any]]:
        return self._list_blobs("claims")

    # ----- Claim view counter (Sprint 22) -----
    def bump_claim_view(self, claim_id: str) -> int:
        """Atomically increment and return the new count.

        UPSERT pattern keeps it to one round-trip even for the first
        view. Doesn't validate that the claim exists — if a stale
        client polls a deleted claim_id, we count it (zero downside,
        and we want to know if it's happening)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO claim_views (claim_id, count)
                VALUES (?, 1)
                ON CONFLICT(claim_id) DO UPDATE SET count = count + 1
                """,
                (claim_id,),
            )
            row = self._conn.execute(
                "SELECT count FROM claim_views WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            self._conn.commit()
            return int(row[0]) if row else 0

    def get_claim_views(self, claim_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM claim_views WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def list_claim_views(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT claim_id, count FROM claim_views"
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def add_annotation(self, ann: dict[str, Any]) -> None:
        self._add_blob("annotations", ann["id"], ann)

    def get_annotation(self, ann_id: str) -> dict[str, Any] | None:
        return self._get_blob("annotations", ann_id)

    def list_annotations(self) -> list[dict[str, Any]]:
        return self._list_blobs("annotations")

    # ----- Sources / snapshot blobs -----
    def save_source(self, paper_id: str, blob: bytes) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sources(paper_id, blob) VALUES (?, ?)",
                (paper_id, blob),
            )
            self._conn.commit()
        return f"/api/v0/papers/{paper_id}/source"

    def load_source(self, paper_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT blob FROM sources WHERE paper_id = ?", (paper_id,)
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    def save_rendered_pdf(self, paper_id: str, blob: bytes) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO rendered_pdfs(paper_id, blob) VALUES (?, ?)",
                (paper_id, blob),
            )
            self._conn.commit()
        return f"/api/v0/papers/{paper_id}/pdf"

    def load_rendered_pdf(self, paper_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT blob FROM rendered_pdfs WHERE paper_id = ?", (paper_id,)
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    def save_snapshot_blob(self, snapshot_id: str, blob: bytes) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshot_blobs(snapshot_id, blob) "
                "VALUES (?, ?)",
                (snapshot_id, blob),
            )
            self._conn.commit()
        return f"/api/v0/snapshots/{snapshot_id}"

    def load_snapshot_blob(self, snapshot_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT blob FROM snapshot_blobs WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    # ----- Idempotency -----
    def get_idempotency(self, token: str, key: str) -> IdempotencyEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body_sha256, response_status, response_body, "
                "created_at_unix FROM idempotency WHERE token = ? AND key = ?",
                (token, key),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyEntry(
            body_sha256=row[0],
            response_status=int(row[1]),
            response_body=json.loads(row[2]),
            created_at_unix=int(row[3]),
        )

    def add_idempotency(
        self, token: str, key: str, entry: IdempotencyEntry
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO idempotency(token, key, body_sha256, "
                "response_status, response_body, created_at_unix) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token,
                    key,
                    entry.body_sha256,
                    entry.response_status,
                    json.dumps(entry.response_body),
                    entry.created_at_unix,
                ),
            )
            self._conn.commit()

    # ----- Snapshots -----
    def latest_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'latest_snapshot'"
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])  # type: ignore[no-any-return]

    def set_latest_snapshot(self, manifest: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('latest_snapshot', ?)",
                (json.dumps(manifest),),
            )
            self._conn.commit()

    # ----- Rate limiting -----
    def record_request(self, token_or_ip: str, now_unix: int) -> int:
        cutoff = now_unix - 60
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamps FROM rate_window WHERE bucket = ?",
                (token_or_ip,),
            ).fetchone()
            if row is None:
                window: list[int] = []
            else:
                window = json.loads(row[0])
            while window and window[0] < cutoff:
                window.pop(0)
            window.append(now_unix)
            self._conn.execute(
                "INSERT OR REPLACE INTO rate_window(bucket, timestamps) "
                "VALUES (?, ?)",
                (token_or_ip, json.dumps(window)),
            )
            self._conn.commit()
            return len(window)

    # ----- Corpus management -----
    def clear_corpus(self) -> None:
        """Truncate the read-corpus tables. Used by seed-store --reset."""
        with self._lock:
            for table in (
                "papers",
                "cirs",
                "claims",
                "annotations",
                "sources",
                "rendered_pdfs",
                # Sprint 22: claim ids can change between releases when the
                # parser re-prefixes them (Sprint 18's `paper-XYZ:c1` rebase),
                # so the view counter MUST be reset too. Otherwise the
                # leaderboard surfaces dead claim ids.
                "claim_views",
            ):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()


# ----- Identity (de)serialisation -----


def _identity_type_str(identity: Identity) -> str:
    if isinstance(identity, OrcidIdentity):
        return "orcid"
    if isinstance(identity, AgentIdentity):
        return "agent"
    return "anonymous"


def _identity_to_dict(identity: Identity) -> dict[str, Any]:
    return asdict(identity)


def _identity_from_dict(identity_type: str, payload: dict[str, Any]) -> Identity:
    if identity_type == "orcid":
        return OrcidIdentity(**payload)
    if identity_type == "agent":
        return AgentIdentity(**payload)
    return AnonymousIdentity(**payload)
