# Changelog

All notable changes to `rrxiv-python` are recorded here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [SemVer](https://semver.org/spec/v2.0.0.html) once it ships its first stable release. While in pre-1.0, breaking changes can land at any minor version.

## [0.2.0] — 2026-07-14

The 2026-07 corpus-enrichment release: RRP-0030 claim authoring keys in the
parser, statement-hygiene fixes, and an agent-drivable ORCID login. This is
the version the published paper repos' tooling targets (`pip install
'rrxiv>=0.2'`).

### Added

- **RRP-0030 claim authoring keys** ([RRP-0030](https://github.com/random-walks/rrxiv/blob/main/proposals/0030-claim-authoring-keys.md), #91). The `claim` environment's optional argument may be a key=value list — `type=` (claim_type), `evidence=` (evidence_type), `confidence=`/`confidence-low=`/`confidence-high=`/`rationale=` (the confidence object), `labels={...}`, and the scope keys `models=`/`datasets=`/`regimes=`/`assumptions=`. Backwards compatible (a plain title stays a title); validation is loud — unknown keys, bad enums, or out-of-range floats fail the parse (`ClaimKeyError`) instead of silently defaulting. Pairs with `rrxiv.cls` 0.8+.
- **Agent-drivable ORCID login.** `rrxiv login orcid --print-url` emits the paste-flow authorization URL and exits (hand the link to a human); `--code <RRXIV-XXXX-XXXX>` redeems the paste code non-interactively and persists the bearer. Against a remote server the paste-back flow is now the **default** (the local-listener flow only works for dev ORCID apps registered with localhost redirect URIs — exactly the `redirect_uri does not match` wall the old default hit against rrxiv.com); force the listener with `--listener`. Listener timeout now suggests the paste flow.

### Fixed

- **Edge macros no longer leak into claim statements** (#93). `\dependson`/`\supports`/`\contradicts`/`\extendsclaim` inside a claim body are stripped from the CIR `statement` (previously only proofs were cleaned); also fixed `_EDGE_MACRO_RE` matching `extends` instead of the real `\extendsclaim`, so extends-edges are now stripped from proofs too. Live corpora clean up on their next reparse/submission.
- **Line-wrapped RRP-0030 key lists parse correctly** (#92). pylatexenc drops brace-containing optional args into the body; the recovery now spans newlines, and a non-key bracket prefix (e.g. a `[1]` citation opening the prose) stays in the statement.
- **`login status` sees keyring-held credentials.** The keyring backend now maintains the metadata index file (no secrets — tokens/keys redacted as `<in-keyring>`) that `login status`/`stored_servers` read; previously every keyring-stored identity was invisible to `status`.
- Dead assignment in `papers/diff.py`; `rrxiv diff` CLI + `GET /papers/{id}/errata` documented (#90).

### Changed

- Vendored `reproducibility_manifest.schema.json` 0.1.0 → 0.1.1 (#94, rrxiv#59): manifests may carry the `$schema` self-identification key shown in RRP-0019's example.

## [0.1.0] — 2026-07-12

First public release. Everything below ships in 0.1.0 (the package was
developed under `[Unreleased]` and had never been published to PyPI, so
all of it — the initial scaffolding through the productionization
sprint — lands together in the first tagged release).

### Added

#### SRV sprint (May 2026)

- **`rrxiv submit` CLI** ([RRP-0016](https://github.com/random-walks/rrxiv/blob/main/proposals/0016-submission-request-schema.md), [RRP-0017](https://github.com/random-walks/rrxiv/blob/main/proposals/0017-revision-flow-and-diff.md)). New Typer subcommand `rrxiv submit <cir.json> <bundle.tar.gz>` posts to `/api/v0/submissions`. Resolves identity from the keyring (ORCID bearer or agent keypair via `rrxiv login`), computes the SHA-256 bundle hash, and supports `--revision-of <prior_paper_id>`, `--revision-summary <text>` (or `--revision-summary-file`), `--dry-run`, and `--identity {orcid|agent}`. Output: id_slug, paper_id, retrieval_uri, view URL; for revisions also a one-line diff summary (claims +N/-M/~K plus abstract flag). Supports `--json` for machine consumption. New module `rrxiv.cli.submit`.
- **Submission `dry_run` + `client_compile_hash`** ([RRP-0016](https://github.com/random-walks/rrxiv/blob/main/proposals/0016-submission-request-schema.md)). `POST /api/v0/submissions` now accepts `dry_run`, `client_compile_hash`, and `revision_summary` form fields. Dry-run validates + compiles + parses + diffs without persisting (returns 200 + `would_persist`); real submissions return 201 with extended body (`version`, `previous_version`, `id_slug`, `revision_diff`, `dry_run: false`). Hash mismatch → 400 `bundle_hash_mismatch`. Unknown `previous_version` → 400 `previous_version_not_found`.
- **Revision diff endpoint** ([RRP-0017](https://github.com/random-walks/rrxiv/blob/main/proposals/0017-revision-flow-and-diff.md)). New `GET /api/v0/papers/{id}/diff?from=<prior>` returns a `RevisionDiff` (added/removed/modified claims with word-level statement + proof hunks, abstract/topics deltas). Lineage check rejects unrelated papers with 400 `papers_not_in_same_lineage`. Same diff is attached inline to revision submission responses. New `rrxiv.server.papers.diff` module (pure functions: `compute_revision_diff`, `claim_local_id`, `papers_in_same_lineage`).
- **Revision summary annotation synthesis** ([RRP-0017](https://github.com/random-walks/rrxiv/blob/main/proposals/0017-revision-flow-and-diff.md)). When `revision_summary` form field is provided on a revision submission, the server synthesises a `revision_summary` annotation attached to the new paper, authored by the submitting identity. Author may supersede with richer structured highlights.
- **Errata listing** ([RRP-0017](https://github.com/random-walks/rrxiv/blob/main/proposals/0017-revision-flow-and-diff.md) companion). New `GET /api/v0/papers/{id}/errata` returns paginated erratum annotations for a paper, newest first.
- **Annotation threads** ([RRP-0018](https://github.com/random-walks/rrxiv/blob/main/proposals/0018-annotation-threads.md)). Annotations accept an `in_reply_to` field. Server validates: target exists (else 400 `in_reply_to_not_found`); same paper artefact (else `in_reply_to_artefact_mismatch`); not self-reply (`in_reply_to_self`). New `GET /api/v0/annotations/{id}/replies` returns direct children oldest-first. New `rrxiv.server.annotations.threads` module.
- **Server-derived `replication_status`** ([RRP-0019](https://github.com/random-walks/rrxiv/blob/main/proposals/0019-reproducibility-manifests.md), [RRP-0020](https://github.com/random-walks/rrxiv/blob/main/proposals/0020-author-claim-retraction.md)). On every claim read path the server recomputes `claim.replication_status` from accumulated annotations: independent-replication count ≥ per-discipline quorum → `replicated`; contradicts ≥ supports → `contradicted`; any non-superseded non-lifted `claim_retraction` → `retracted` (highest precedence). Quorum defaults: 1 math/formal, 2 algo/crypto, 3 ML/experimental, 5 behavioural. v0.x compromise: with zero annotations the persisted author-set value is honoured (Euclid corpus stays working without backfill). New `rrxiv.server.claims.replication` module.
- **New annotation types**: `revision_summary` (RRP-0017) and `claim_retraction` (RRP-0020) — both surface through the generic `POST /annotations` flow with structured payloads. Retraction's lift convention (`comment.in_reply_to + structured_payload.lifts_retraction=true` from the same author identity) reverts derivation to the normal rule.
- **Refined `replication` annotation payload** (RRP-0019): `reproduction_kind` discriminates `fresh_replication` from `reproduction_from_artifacts`; optional `confidence_interval`, `reproducibility_manifest_uri`, `reproducibility_manifest_hash` fields; backward-compat default of `fresh_replication` for pre-RRP annotations.
- **Author claim reproducibility manifest** (RRP-0019): `Claim.reproducibility = { manifest_uri, manifest_hash }` available end-to-end through the regenerated `rrxiv.models.ClaimReproducibility`.
- **Schema sync** (auto): submitted_request, revision_diff, reproducibility_manifest schemas mirrored from rrxiv into `src/rrxiv/_schemas/` + pydantic models regenerated under `src/rrxiv/models/_generated/`. `rrxiv.models` re-exports `SubmissionRequest`, `RevisionDiff`, `ReproducibilityManifest`, `VersionRef`, `AddedClaim`, `RemovedClaim`, `ModifiedClaim`, `DiffHunk`, `TextDiff`, `ClaimReproducibility`, `ExpectedOutput`, plus supporting enums.
- **Test coverage** (`tests/test_srv_phase2.py`): 21 new tests for diff computation, diff endpoint, errata listing, in_reply_to validation, replies endpoint, retraction derivation, retraction lift, quorum lookup, dry-run, hash mismatch, and revision_summary synthesis.

#### Earlier in [Unreleased]

- **Paper list-item projection** ([RRP-0012](https://github.com/random-walks/rrxiv/blob/main/proposals/0012-paper-list-item-projection.md)). New `rrxiv.server.papers.projection` module computes the aggregate `stats` block (claims, replicated, contradicted, contested, untested counts plus paper-level rollup status) from claims + annotations. `GET /api/v0/papers` and `GET /api/v0/papers/{id}?include=stats` return the `PaperListItem` shape. New `rrxiv.models.PaperListItem`, `Stats`, `PaperStatus` regenerated from the new schema.
- **Server-minted `id_slug`** ([RRP-0013](https://github.com/random-walks/rrxiv/blob/main/proposals/0013-id-slug.md)). `POST /api/v0/submissions` mints a `rrxiv:YYMM.NNNNN` slug for each new paper; revisions inherit. Paper detail endpoints accept both the UUIDv7 `id` and the slug. New `rrxiv.server.papers.slug` module with `mint_slug`, `is_slug`, `find_paper_by_slug`.
- **Discovery endpoints**: `GET /api/v0/scopes`, `GET /api/v0/topics`, `GET /api/v0/claims/top`. Scopes are instance metadata (the five UI-discovery slices: `active`/`agent`/`human`/`contested`/`fresh` plus `all`). Topics are derived from the union of `paper.topics[]`. Top claims is a v0.1 stub ranking pending real telemetry.
- **Per-paper read endpoints**: `GET /api/v0/papers/{id}/claims`, `GET /api/v0/papers/{id}/related`, `GET /api/v0/papers/{id}/stats`. Related uses topic-overlap Jaccard for v0.1.
- **Filtered list endpoints**: `GET /api/v0/papers?scope=<id>&topic=<topic>`. `GET /api/v0/search/papers` accepts the full filter set (`scope`, `topic`, `author`, `status`, `claims_min`, `submitted_from`, `submitted_to`, `sort`).
- **Configurable CORS allowlist** via `RRXIV_CORS_ORIGINS=https://...,https://...`. Empty (dev default) → `*`. Production deployments lock down.
- **`rrxiv seed-store --from <dir> --store <url>` CLI** for bulk-loading CIRs into a store, bypassing `/submissions`. Idempotent on rebuild. Used by the production Dockerfile to bake the seed corpus into the image.
- **Dockerfile + fly.toml** for Fly.io deployments. SQLite on a persistent volume mount at `/data`. First-boot entrypoint seeds the database from the bundled `seed/` directory.
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

#### Productionization sprint (July 2026)

- **Server-side paper-id minting (security)** ([RRP-0029](https://github.com/random-walks/rrxiv/blob/main/proposals/0029-paper-id-uuidv7.md)). `POST /api/v0/submissions` now always mints the UUIDv7 `paper.id` server-side for new (non-revision) submissions and ignores any client-supplied CIR `id`. Previously a submitter could silently overwrite **any** existing paper — including the seeded corpus — by echoing its id, because both stores upsert without an existence check and `rrxiv parse` always emits an id (the tex file stem, e.g. `main`). Revisions still legitimately target an existing id via `previous_version`.
- **ORCID login `redirect_uri` threading** (OAuth [RFC 6749 §4.1.3](https://datatracker.ietf.org/doc/html/rfc6749#section-4.1.3)). The CLI loopback and paste flows now thread the `redirect_uri` they authorized with all the way into the ORCID token exchange — `exchange_orcid_code` gained a `redirect_uri` param, and `/auth/orcid/render` exchanges the code with its own URL — instead of falling back to the server's `RRXIV_ORCID_REDIRECT_URI`. Fixes the 401 that mismatch caused against production.
- **`rrxiv submit` view link** now points at the web host (`rrxiv.com/papers/<slug>`), not the API host (`api.rrxiv.com`), which 404'd. Client-side host mapping only; no server change.
- **`rrxiv seed-store --preserve-community`** — new opt-in flag (mutually exclusive with `--reset`) to reseed the canonical corpus on a live instance while leaving every externally submitted paper and **all** annotations intact. Replaces only the incoming seed papers via the new `Store.replace_seed_papers` (sqlite + memory). `--reset` keeps its full-clear behaviour for dev use.

#### Parser / schema

- DOI regex in `citation.schema.json` now allows lowercase letters (real-world DOIs are mixed case).
- Citation field URL strips a wrapping `\url{...}` macro so it passes pydantic `AnyUrl` validation.

### Initial scaffolding

The starting point 0.1.0 was built on:

- Pydantic v2 models generated from the rrxiv JSON Schemas (paper, claim, annotation, citation, cir).
- TeX parser v0: regex-based walker, sidecar reader, build_cir() producing a validated CIR.
- `rrxiv parse` and `rrxiv validate` CLI commands.
- Schema sync mechanism (`scripts/sync_schemas.sh`) and codegen mechanism (`scripts/regen_models.sh`).
- MIT license.
