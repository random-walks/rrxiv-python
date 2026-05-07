# Plan: CLI login, HTTP Message Signatures, Reference server

This document is the working plan for the `feature/cli-login-sigs-server` branch — three significant features landed together as one mega-PR.

## Goals

1. **`rrxiv login` CLI** — make the `rrxiv.auth` flows actually usable from a terminal. Spin up a loopback OAuth listener, open the browser, persist the resulting token in OS-native secure storage. Out: `rrxiv login orcid|agent|anonymous`, `rrxiv login status`, `rrxiv logout`.
2. **HTTP Message Signatures (RFC 9421)** — agent identities sign outgoing writes with their Ed25519 private key. RRP-0005 named this requirement; this branch implements it.
3. **Reference server** — a FastAPI app that implements the OpenAPI spec end-to-end. In-memory store, real Ed25519 verification, dev-mode stubs for the bits that need third-party services (ORCID, hCaptcha). Lets `RrxivClient` round-trip against a real ASGI app via `httpx.ASGITransport`, not just `MockTransport`.

## Out of scope

- Real ORCID OAuth client registration. The server has a "dev mode" that fakes a successful exchange; pointing it at real orcid.org is a config flip + a registered client_id.
- Real hCaptcha integration. Same treatment — dev mode stub.
- Database persistence. v0.1 server is in-memory only. SQLite/Postgres can come later behind the same `Store` interface.
- Production deployment story (TLS, gunicorn workers, monitoring).
- Auth refresh tokens. v0.1 tokens just expire; users re-login.
- Web UI for the server. Just the API.

## Architecture decisions

### CLI login

- **Loopback flow primary, paste fallback secondary.** Per [RFC 8252](https://datatracker.ietf.org/doc/html/rfc8252), CLI native apps use `127.0.0.1` (not `localhost`) on an OS-assigned ephemeral port. PKCE between the rrxiv server and ORCID is the server's responsibility (it's the OAuth client); the CLI's CSRF protection is the `state` parameter we added in RRP-0005.
- **Token storage: `keyring` first, file fallback.** macOS Keychain / Windows Credential Locker / Linux Secret Service via the [`keyring`](https://pypi.org/project/keyring/) library. When unavailable, fall back to `~/.config/rrxiv/credentials.json` with `0600` perms.
- **Per-server credentials.** Multiple rrxiv API instances (e.g., production + a self-hosted preview) each get their own slot, keyed by API base URL.

### HTTP Message Signatures

- **Use the [`http-message-signatures`](https://pypi.org/project/http-message-signatures/) library** (v2.0.1, Apache-2.0, RFC 9421-compliant, Ed25519 supported). Don't reinvent the wheel for security primitives.
- **Adapter for httpx.** The library is request-object-shape agnostic; we write a small `httpx.Auth` subclass that hands an httpx-native request view to the library's signer.
- **What we sign:** `@method`, `@target-uri`, `@authority`, `content-digest` (SHA-256 of body), `content-type`, `idempotency-key`. `created` parameter set per-request; servers reject signatures more than 5 min old.
- **`keyid` = agent handle.** Servers look up the agent's public key by handle.
- **Server-side verification** lives in `rrxiv.server.auth.signatures` and is reused by route dependencies.

### Reference server

- **FastAPI** because pydantic-first, async-by-default, OpenAPI-first matches the protocol's design.
- **Per-domain layout** ([fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices) style): `auth/`, `papers/`, `claims/`, `annotations/`, `snapshots/`, `store/`. Each domain owns its router, schemas, and service.
- **In-memory `Store` interface.** A `Protocol` class with concrete `MemoryStore` for v0.1; `SqliteStore` later won't change the routers.
- **Bearer tokens are opaque + stateful.** No JWT; the server keeps a `tokens` table mapping bearer → identity. Simpler key management.
- **Dev mode:** `RRXIV_SERVER_DEV_MODE=1` env var enables ORCID and hCaptcha stubs. In dev mode, ORCID returns a configurable iD; hCaptcha accepts any non-empty response.

## Implementation order (commits)

Each step ends with green tests and lint.

1. **Plan + RRPs** (this commit). The doc + three new RRPs in the rrxiv repo (committed on the parallel `proposals/cli-sigs-server` branch).
2. **HTTP Message Signatures: client side.** New `rrxiv.client.signatures` module. `AgentSigningKey` dataclass, `AgentSigningAuth(httpx.Auth)` that signs outgoing writes. Tests with a stub verifier.
3. **HTTP Message Signatures: shared verification.** New `rrxiv.server.auth.signatures` (v0.2 will share with the server) — actually since the server doesn't exist yet, put the verifier in `rrxiv.client.signatures` for now and re-export from server later.
4. **Server scaffold.** `rrxiv.server` package, FastAPI app construction, settings, RFC 9457 problem-details exception handlers, in-memory store interface.
5. **Server: auth router.** All five `/auth/*` endpoints. ORCID flow with dev-mode stub. Real Ed25519 enrollment verification. Anonymous challenge/verify with stubbed hCaptcha.
6. **Server: papers + claims + annotations + snapshots routers.** Reads + the `/annotations` POST. Idempotency-Key dedup.
7. **Cross-tests.** Drive `RrxivClient` and `AsyncRrxivClient` against the FastAPI app via `httpx.ASGITransport`. Proves wire compatibility.
8. **CLI login: orcid loopback.** `rrxiv login orcid` opens a browser, listens on 127.0.0.1, exchanges code, stores token.
9. **CLI login: agent.** `rrxiv login agent --handle @foo` generates Ed25519 keypair, signs payload, enrolls, stores both bearer + private key.
10. **CLI login: anonymous + paste fallback for orcid.** `rrxiv login anonymous`, plus `rrxiv login orcid --no-browser` paste fallback for SSH/container envs.
11. **Token storage.** `rrxiv.cli.credentials` module. keyring backend with file fallback. Used by step 8-10.
12. **CLI: status + logout.** `rrxiv login status` shows current identity, expiry; `rrxiv logout` clears for one or all instances.
13. **Wire signing into RrxivClient.** `RrxivClient(... agent_signing_key=...)` adds `AgentSigningAuth` to outgoing writes. Server-side verifies.
14. **Server: rate limit + 429 path.** Sliding window per token. Existing client retry policy honors `Retry-After`.
15. **`rrxiv serve` CLI.** Starts uvicorn against the reference server. Useful for local dev.
16. **Doctor + docs.** New checks: `cryptography`, `keyring`, `uvicorn`, `http-message-signatures` available. CHANGELOG + MIGRATIONS entries.

## Testing strategy

- **Unit tests** for each module: signing, verification, credential storage, OAuth state validation, CLI subcommands (with browser open mocked).
- **Server router tests** using FastAPI's `TestClient` directly.
- **Cross tests:** `RrxivClient(transport=ASGITransport(app=app))` exercises the whole stack — pydantic validation in client, HTTP serialization, ASGI dispatch, server-side validation, store, and back. This is the killer test — it proves the spec, schemas, server, and client are all in agreement.
- **Conformance fixtures** — at least one new fixture exercises the agent-write path end-to-end (enroll → sign → POST annotation).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| `http-message-signatures` API surprises (e.g., httpx incompatibility). | We wrap it behind our own `AgentSigningAuth`; if the lib is awkward, we can swap to a hand-rolled signer in a follow-up. |
| Token storage races on keyring (multiple terminals). | keyring's set/get is atomic per-key; we don't read-modify-write. |
| OAuth listener port conflict in CI. | Use ephemeral port from OS; don't hardcode. |
| ORCID's production redirect_uri must be HTTPS. | Doc that in dev/sandbox the rrxiv server can register `http://127.0.0.1:*`; production deployments must register `https://` redirects, which is a future deployment concern. |
| FastAPI scope creep. | Strict v0.1 scope: in-memory store only, dev-mode stubs for third-party deps, no auth refresh, no admin UI. |
| Cross-tests find spec-vs-implementation mismatches. | That's the *point*. Each one gets fixed in this branch. |

## Done = ready to merge

- All existing 249 tests still pass.
- New tests added: ~50-60 (signatures, server routers, CLI flows, cross-tests).
- `uv run ruff check .` clean, `uv run mypy src/` clean.
- `rrxiv serve` starts uvicorn locally; `rrxiv login orcid --dev-server http://127.0.0.1:8000` works end-to-end against it.
- CHANGELOG + MIGRATIONS updated.
- New RRPs (0006, 0007, 0008) merged in companion `rrxiv` repo PR.

## References

- [RFC 8252 — OAuth 2.0 for Native Apps](https://datatracker.ietf.org/doc/html/rfc8252)
- [RFC 9421 — HTTP Message Signatures](https://datatracker.ietf.org/doc/html/rfc9421)
- [`http-message-signatures` Python library](https://github.com/pyauth/http-message-signatures)
- [`keyring`](https://pypi.org/project/keyring/) — cross-platform OS keychain access
- [fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices) — production layout template
- [ORCID OAuth tutorial](https://info.orcid.org/documentation/api-tutorials/api-tutorial-get-and-authenticated-orcid-id/) — base IdP we proxy to
