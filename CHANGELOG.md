# Changelog

All notable changes to `rrxiv-python` are recorded here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [SemVer](https://semver.org/spec/v2.0.0.html) once it ships its first stable release. While in pre-1.0, breaking changes can land at any minor version.

## [Unreleased]

### Added

- `rrxiv conformance <server-url>` — CLI subcommand running the canonical 9-step end-to-end story (enroll → submit → list → search → annotate → snapshot → verify) against any rrxiv server. Useful for validating other-language client+server pairs.
- `rrxiv.testing.live_server` — pytest fixture spinning up a real uvicorn-backed reference server on an ephemeral 127.0.0.1 port. Importable by downstream client packages.
- **SQLite persistent store** ([RRP-0011](https://github.com/random-walks/rrxiv/blob/main/proposals/0011-sqlite-store.md)). New `rrxiv.server.store.SqliteStore` implementing the `Store` Protocol; configured via `RRXIV_STORE_URL=sqlite:///path/to/db.sqlite` or `rrxiv serve --store sqlite:///...`. `memory://` remains the default. Same routers, swappable backend.
- **Agent Ed25519 key rotation** ([RRP-0010](https://github.com/random-walks/rrxiv/blob/main/proposals/0010-agent-key-rotation.md)). `POST /auth/agent/{handle}/rotate-key` requires both an old-key transport signature and an inline new-key signature (proof of possession). Server atomically replaces the registered public key. New `rrxiv.auth.agent.{rotate_agent_key, build_rotation_payload}` helpers.
- **Bearer-token refresh** ([RRP-0009](https://github.com/random-walks/rrxiv/blob/main/proposals/0009-refresh-tokens.md)). `POST /auth/refresh` exchanges a still-valid bearer for a fresh one. Old token revoked atomically. New `rrxiv.auth.refresh_bearer` client helper. Anonymous tokens cannot be refreshed.
- **Server-side render endpoints** for the paste-back flows (RRP-0006 follow-up): `GET /auth/orcid/render` (mints a one-time paste code and shows it inline) and `GET /auth/anonymous/render` (hosts the hCaptcha widget). Closes the v0.1 UX gap where the CLI told the user to open a URL that 404'd.
- **Real ORCID OAuth + real hCaptcha** integrations on the reference server. Server-side httpx call to `orcid_token_url` and `api.hcaptcha.com/siteverify` when `dev_mode=False`. Tests mock the httpx call. Configuration via `RRXIV_ORCID_CLIENT_ID`, `RRXIV_ORCID_CLIENT_SECRET`, `RRXIV_ORCID_REDIRECT_URI`, `RRXIV_HCAPTCHA_SECRET`.
- **Submissions + sources + versions + search + snapshot creation** API routes, completing the OpenAPI surface: `POST /submissions` (multipart cir+bundle), `GET /papers/{id}/source`, `GET /papers/{id}/versions`, `GET /search/papers`, `GET /search/claims`, `POST /snapshots`, `GET /snapshots/{id}/blob`. Submissions are validated against `rrxiv.models.CIR` (strict). Anonymous identities forbidden from writes.
- **Strict annotation validation**: `POST /annotations` validates against `rrxiv.models.Annotation` (which embeds the per-type `structured_payload` validators). Cross-paper claim ID validation: `target_type=claim` requires `<paper_id>:c<n>` shape and existence; `target_type=paper` requires the paper to exist.
- **Signature-verification ASGI middleware** (RRP-0007 hardening). Replaces the dependency-based body-consume-and-restore with an ASGI middleware that runs before FastAPI's body parser. Multipart uploads (`POST /submissions`) now work with agent signatures.
- **End-to-end conformance fixture** (`tests/test_conformance_e2e.py`) — the canonical 9-step story against a live uvicorn server.
- `rrxiv login {orcid|agent|anonymous}` CLI subcommands per [RRP-0006](https://github.com/random-walks/rrxiv/blob/main/proposals/0006-cli-login.md). ORCID flow uses RFC 8252-style loopback OAuth with `--no-browser` paste fallback for SSH/container envs. Agent flow generates an Ed25519 keypair locally, signs the canonical enrollment payload, and persists both the bearer and the private key. `rrxiv login status` lists stored identities; `rrxiv logout` clears them.
- `rrxiv.cli.credentials` — OS-native keyring storage (macOS Keychain / Windows Credential Locker / Linux Secret Service) via `keyring`, with a 0600-file fallback at `~/.config/rrxiv/credentials.json` for headless environments. Multi-server, multi-identity-type slots so a developer can be logged in to multiple instances simultaneously. Override via `RRXIV_CRED_BACKEND={keyring|file}` and `RRXIV_CRED_DIR`.
- `rrxiv.client.signatures` — HTTP Message Signatures (RFC 9421) for agent writes per [RRP-0007](https://github.com/random-walks/rrxiv/blob/main/proposals/0007-message-signatures.md). `AgentSigningKey` wraps an Ed25519 keypair + handle; `AgentSigningAuth(httpx.Auth)` signs outgoing writes in place. Wired into `RrxivClient` and `AsyncRrxivClient` via `agent_signing_key=` constructor param. Server-side verifier (`verify_request_signature`) is reused by the reference server.
- `rrxiv.server` — FastAPI **reference server** per [RRP-0008](https://github.com/random-walks/rrxiv/blob/main/proposals/0008-reference-server.md). Per-domain layout (`auth/`, `papers/`, `claims/`, `annotations/`, `snapshots/`, `store/`), in-memory storage, dev-mode stubs for ORCID OAuth + hCaptcha, real Ed25519 verification (no shortcuts on the crypto path). RFC 9457 problem-details on errors. Idempotency-Key dedup. Sliding-window rate limiting. Cross-tests drive `RrxivClient` against the FastAPI app via Starlette's TestClient transport — proves wire compatibility end-to-end.
- `rrxiv serve` CLI subcommand — start the reference server with uvicorn. Defaults: `127.0.0.1:8000`, dev mode on.
- New `[agent]`, `[cli]`, `[server]` optional extras. `[dev]` includes all three so tests run against the full stack.
- New doctor checks: `[agent]`/`[cli]`/`[server]` extras present, keyring backend usable.
- `rrxiv.auth` module — token-acquisition flows for ORCID OAuth, agent enrollment (Ed25519), and anonymous attestation (hCaptcha) per [RRP-0005](https://github.com/random-walks/rrxiv/blob/main/proposals/0005-token-acquisition.md). Includes helpers (`build_orcid_authorization_url`, `exchange_orcid_code`, `enroll_agent`, `request_anonymous_challenge`, `verify_anonymous_challenge`) and request/response dataclasses for each flow. Optional `[agent]` extra pulls in `cryptography` for keypair signing.
- `rrxiv.client.errors.raise_for_status` — public status-code-to-exception mapper, shared by the sync client, async client, and auth flow helpers.
- `MockRrxivServer` handlers for the five `/auth/*` endpoints (orcid callback, agent enroll, anonymous challenge/verify) so client tests can exercise wire shape without a real server.
- TeX parser now uses `pylatexenc.latexwalker` (AST-based) per [RRP-0004](https://github.com/random-walks/rrxiv/blob/main/proposals/0004-tex-parser-ast.md) — comments and math mode no longer leak into extractors; multi-optional-arg macros parse correctly.
- `rrxiv doctor` — health check for the package + environment.
- `rrxiv diff` — semantic diff between two CIRs (claims, edges, citations, annotations, top-level fields).
- `rrxiv init` — scaffold a new rrxiv paper directory with a bundled `rrxiv.cls`.
- Standalone `Section` and `Figure` pydantic models (was: `$defs` of `cir.schema.json`).
- `AsyncRrxivClient` — `httpx.AsyncClient`-backed mirror of the sync client, with `async for paper in client.iter_papers(...)` style helpers.
- Configurable opt-in retry policy on the HTTP client; default = 3 retries / 30s total budget / no 5xx retry. `Retry-After` honoured; jittered exponential backoff otherwise.
- `rrxiv.annotations` module with per-type `structured_payload` validators per `spec/0006-annotations.md`. `rrxiv annotation validate <file>` CLI subcommand.
- `rrxiv.graph.ClaimGraph` — typed directed multigraph with traversal, cycle detection, Mermaid / DOT / JSON output. `rrxiv graph` CLI.
- TeX-to-text cleaning of CIR title/abstract/claim statements; the canonical fields no longer carry `\Large`, `\\[0.2em]`, `\texttt{...}` etc.
- `\bibitem` extraction from inline `thebibliography` blocks (the rrxiv whitepaper's bibliography now resolves into the CIR).
- Sidecar parser back-compat: accepts both the post-rename `RRXIV:` prefix and the pre-rename `RRVIX:` prefix (with a `DeprecationWarning`).

### Changed

- **Project renamed: `rrvix` → `rrxiv`.** Every Python identifier (`from rrvix import ...` → `from rrxiv import ...`), the CLI command name (`rrvix parse` → `rrxiv parse`), the package name on PyPI (when published) all moved. The legacy `RRVIX:` sidecar prefix and `.rrvix.aux` extension are still parsed, with a deprecation warning.
- HTTP client errors now follow [RFC 9457 Problem Details](https://datatracker.ietf.org/doc/rfc9457/) for the parsed body when the server provides one.
- TeX parser now applies the TeX-to-text cleaner to title, abstract, and claim statements; CIRs read like prose.
- Edge marker delimiter in the cls is `|` (was `:`); the parser accepts both, with a deprecation warning on the colon form (per [RRP-0002](https://github.com/random-walks/rrxiv/blob/main/proposals/0002-edge-marker-delimiter.md)).

### Fixed

- DOI regex in `citation.schema.json` now allows lowercase letters (real-world DOIs are mixed case).
- Citation field URL strips a wrapping `\url{...}` macro so it passes pydantic `AnyUrl` validation.

## [0.1.0] — initial scaffolding

- Pydantic v2 models generated from the rrxiv JSON Schemas (paper, claim, annotation, citation, cir).
- TeX parser v0: regex-based walker, sidecar reader, build_cir() producing a validated CIR.
- `rrxiv parse` and `rrxiv validate` CLI commands.
- Schema sync mechanism (`scripts/sync_schemas.sh`) and codegen mechanism (`scripts/regen_models.sh`).
- MIT license.
