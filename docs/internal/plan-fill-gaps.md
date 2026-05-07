# Plan: Fill all v0.1 gaps + RRP open-questions in one go

This is the second-half plan, continuing on `feature/cli-login-sigs-server` (PR #21). The first-half plan (`plan-cli-sigs-server.md`) shipped the CLI login, RFC 9421 signatures, and the FastAPI reference server scaffold. This plan fills the gaps the first-half plan acknowledged and lands the v0.2-bound items the existing RRPs flagged as open questions.

## What's still missing (audit)

### A. UX gaps — advertised, doesn't work end-to-end

These are things the CLI tells the user about that 404 against the reference server:

1. `GET /auth/orcid/render` — the page that displays the paste code after the user completes ORCID OAuth. CLI's `--no-browser` flow refers to it; not implemented.
2. `GET /auth/anonymous/render` — the page that hosts the hCaptcha widget so an anonymous user can solve it. CLI's `login anonymous` flow refers to it; not implemented.
3. Real ORCID OAuth code exchange against orcid.org. Server only handles dev codes; real codes get a 400.
4. Real hCaptcha siteverify call. Server bypasses verification in dev mode; non-dev returns a hard error.

### B. API surface in OpenAPI but not in server

Routes that exist in `schema/api.openapi.yaml` (rrxiv repo) but the FastAPI server doesn't implement:

5. `POST /submissions` — submit a paper (creates a Paper record, kicks off processing).
6. `GET /papers/{id}/source` — download the paper's source archive (tar.gz).
7. `GET /papers/{id}/versions` — paper version history.
8. `GET /search/papers` — full-text-ish search.
9. `GET /search/claims` — search over claims.
10. `POST /snapshots` — server-side snapshot creation. Currently only `GET /snapshots/latest` exists.

### C. Strictness / hardening

11. Annotation creation accepts loose `dict[str, Any]`. Should validate against `rrxiv.models.Annotation` (which embeds the per-type `structured_payload` validators we already have).
12. Better cross-paper claim ID validation (claim IDs follow `<paper_id>:c<n>` per RRP-0001; not enforced server-side).
13. Conformance fixture for the end-to-end agent path: enroll → sign → submit annotation → verify it round-trips.

### D. RRP open questions — punted to v0.2

14. **RRP-0005 §"Open questions" — Refresh tokens.** Today tokens expire and users re-login from scratch. Add `POST /auth/refresh` that takes a still-valid bearer + returns a fresh one with a new TTL.
15. **RRP-0007 §"Open questions" — Agent key rotation.** Today losing an agent's private key means re-enrolling under a fresh handle. Add `POST /auth/agent/{handle}/rotate-key` signed by both the old and new key.
16. **RRP-0008 §"Open questions" — Persistent storage.** Today the server is in-memory. Add a `SqliteStore` that implements the same `Store` protocol — same routers, persistent across restarts.
17. **RRP-0006 §"Open questions" — Device flow (RFC 8628).** Alternative to loopback for environments where neither `127.0.0.1` listening nor browser interaction works (locked-down CI).

### E. Quality of life

18. `rrxiv.testing.live_server` — a pytest fixture other Python clients can import to spin up a reference server in their tests.
19. A real working conformance suite: `rrxiv conformance <server-url>` runs the cross-tests against any server.

## Goals — committed scope for this branch

**Definitely:**

- A1, A2 — render endpoints
- A3, A4 — real ORCID + real hCaptcha (config-driven, dev mode stays on by default)
- B5–B10 — all six missing API routes
- C11, C12, C13 — strict validation + conformance fixture
- D14 — refresh tokens (RRP-0009)
- D15 — agent key rotation (RRP-0010)
- D16 — SQLite store (RRP-0011)

**Probably:**

- D17 — device flow (RRP-0012). Drops if scope balloons.

**Stretch:**

- E18 — `rrxiv.testing.live_server` pytest fixture.
- E19 — `rrxiv conformance` CLI subcommand.

## Architecture decisions

### Render endpoints (A1, A2)

Self-contained HTML+JS pages served from FastAPI via `HTMLResponse`. Templates live as Python string constants in the route module so we don't have to bundle a Jinja directory in the package.

The ORCID render page:

- Shows a paste code shaped like `RRXIV-A4F7-D2K3` (8-char hex with dashes for legibility).
- Generates the paste code on the server, persists it in the `paste_codes` table with the resolved ORCID iD, and renders it inline.
- Style: minimal inline CSS, no external assets, no JS except a "copy to clipboard" button.

The anonymous render page:

- Hosts a static HTML form with the hCaptcha widget (loaded from `https://js.hcaptcha.com/1/api.js`).
- Requires the `site_key` and `challenge_id` query params.
- After the widget produces a response token, the page displays it for paste-back. (Not auto-submitting — this flow is for the user to copy-paste into a CLI on a different machine.)

### Real ORCID OAuth (A3)

Server-side `_exchange_with_orcid(code)` helper that POSTs to ORCID's token endpoint with `client_id`, `client_secret`, `code`, `grant_type=authorization_code`, `redirect_uri`. Pulls these from `ServerSettings`. On success, returns the ORCID iD.

Configuration: `RRXIV_ORCID_CLIENT_ID`, `RRXIV_ORCID_CLIENT_SECRET`, `RRXIV_ORCID_REDIRECT_URI`. If any is missing and `dev_mode=False`, the route returns a clear configuration error.

Tests: don't actually hit orcid.org in CI; instead, mock the httpx call with a fixture. The dev-mode path stays for `rrxiv serve` quick starts.

### Real hCaptcha verify (A4)

Same shape: `_verify_with_hcaptcha(token)` calls `https://api.hcaptcha.com/siteverify` with the secret + response token. Config: `RRXIV_HCAPTCHA_SECRET`. Tests mock the verify call.

### Submissions (B5)

`POST /submissions` accepts `multipart/form-data` with two parts:

- `metadata` (JSON) — paper metadata matching `paper.schema.json`.
- `source` (binary, optional in v0.1) — source archive bytes.

Server response: `201 { id, version, source_uri }`. The store persists the metadata; the source archive is stored under a configurable path (default `<RRXIV_SNAPSHOT_DIR>/sources/<id>.tar.gz`).

For v0.1 reference server: source storage is the local filesystem. Same `Store` interface gets a new method `save_source(paper_id, bytes) -> uri`.

### Source / versions endpoints (B6, B7)

- `GET /papers/{id}/source` — streams the tar.gz from the configured source dir. 404 if not present.
- `GET /papers/{id}/versions` — returns a list of `{version, submitted_at, source_uri}` items, ordered chronologically.

In-memory v0.1: papers track their version chain in the same record (already in the schema as `previous_version`). Versions are derived from the chain on demand.

### Search (B8, B9)

Naive substring search across in-memory records:

- `/search/papers?q=<term>` — match in `title`, `abstract`, `authors[].name`, `topics`.
- `/search/claims?q=<term>` — match in `statement`.

The OpenAPI spec already documents `q`, `cursor`, `limit`. v0.1 reference server ignores `cursor` (returns all results in `items`, `next_cursor: null`) since result sets stay small.

### Snapshot creation (B10)

`POST /snapshots` requires agent or ORCID auth. Builds a tar.gz of:

- `papers/<id>.json` for each paper in the store.
- `cirs/<id>.json` for each CIR.
- `claims/<id>.json` for each claim.
- `annotations/<id>.json` for each annotation.
- `manifest.json` with timestamp, counts, and a `Content-Digest` over the archive.

Returns the manifest. Sets `latest_snapshot` to point at the new archive.

### Strict annotation validation (C11)

Server's `POST /annotations` runs the body through `rrxiv.models.Annotation.model_validate(body)` before storing. The pydantic model already imports the per-type validators from `rrxiv.annotations`. Errors surface as `422` with the per-field detail.

### Cross-paper claim ID validation (C12)

`POST /annotations` with `target_type=claim` checks the `target_id` shape (`<paper_id>:c<n>`) and that the referenced claim exists. 422 on shape mismatch; 404 on missing claim.

### Conformance fixture (C13)

`tests/test_conformance_e2e.py` — a single end-to-end story:

1. Start the reference server.
2. Enroll an agent.
3. Submit a paper (multipart).
4. Verify it shows up in `GET /papers`.
5. Search for it via `GET /search/papers`.
6. Create an annotation against it as the agent (signed write).
7. Read it back and assert the round-trip.
8. Trigger a snapshot.
9. Verify the snapshot manifest references the paper + annotation.

This is the single test we'd want a downstream client (Rust, Go) to be able to drive against a server — same shape, different driver.

### Refresh tokens — RRP-0009 (D14)

Wire format:

```
POST /auth/refresh
Authorization: Bearer <still-valid-token>
→ 200 { token, expires_in_seconds }
```

Server invariants:

- The current bearer must still be valid.
- Refresh succeeds if the old token is more than 50% through its TTL (avoid wasteful refresh-on-issue).
- New token has the same identity tier; old token is revoked atomically.

Client side: `RrxivClient.refresh_auth()` and an opt-in `auto_refresh=True` flag that proactively refreshes at 80% TTL. Stored bearers in `rrxiv.cli.credentials` get a `refresh()` helper.

### Agent key rotation — RRP-0010 (D15)

Wire format:

```
POST /auth/agent/{handle}/rotate-key
{
  "new_public_key_b64": "...",
  "rotation_payload_b64": "<base64(canonical JSON {handle, new_public_key, issued_at})>",
  "old_signature_b64": "<sig over rotation_payload_b64 with OLD private key>",
  "new_signature_b64": "<sig over rotation_payload_b64 with NEW private key>"
}
→ 200 { handle, public_key_b64, rotated_at_unix }
```

Server verifies both signatures (proof-of-possession of both keys), then atomically replaces the registered public key for the handle. The agent's bearer continues to work; signatures on subsequent writes use the new key.

CLI: `rrxiv agent rotate-key --handle @bot` generates a fresh keypair, builds the rotation payload, signs with both keys, POSTs, and replaces the stored private key on success.

### SQLite persistent store — RRP-0011 (D16)

A `SqliteStore` that implements the `Store` protocol. Tables mirror the dataclasses in `store/protocol.py`. Migrations are stamped via simple `PRAGMA user_version` checks (good enough for v0.1; real Alembic migrations are a future RRP).

`ServerSettings.store_url` chooses the backend: `memory://` (default) or `sqlite:///path/to/db.sqlite`. `rrxiv serve --store sqlite:///./rrxiv.db` for local persistent dev.

All existing routers stay unchanged — they only see the `Store` protocol.

### Device flow — RRP-0012 (D17, "probably")

Wire format per RFC 8628:

- `POST /oauth/device_authorization` → `{ device_code, user_code, verification_uri, interval, expires_in }`
- `POST /oauth/token` (grant_type=device_code) — polled by client, returns 400 with `authorization_pending` until user completes, then `200 { token }`.

`rrxiv login orcid --device` triggers this flow. Useful for SSH sessions, CI environments, anywhere `127.0.0.1` listening doesn't work.

The user opens `verification_uri` (e.g., `https://rrxiv.com/device`), enters the user code (e.g., `WDJB-MJHT`), completes ORCID OAuth in their browser; the CLI's poll picks up the resulting token.

### Live-server fixture (E18, stretch)

`rrxiv.testing.live_server` provides a pytest fixture other Python clients can import:

```python
from rrxiv.testing import live_server

def test_my_thing(live_server):
    # live_server.url, .auth (helper), .stop, etc.
    ...
```

Backed by the same uvicorn-in-a-thread pattern from `tests/test_cli_login.py`.

### Conformance CLI (E19, stretch)

`rrxiv conformance <server-url> [--auth-mode orcid|agent|anonymous]` runs the canonical conformance story (C13) against any URL. Used to validate other-language client+server pairs.

## Implementation order — commits

Each commit ends with green tests + lint + mypy.

### Phase 1 — close UX gaps (Tier 1)

1. **Plan doc.** This file.
2. **`/auth/orcid/render` + paste-back redemption end-to-end.** Server templates, route, paste_code mint. Update `tests/test_cli_login.py::test_login_orcid_paste_fallback` to drive the render endpoint instead of pre-seeding the table.
3. **`/auth/anonymous/render`.** HTML page hosting the hCaptcha widget. The CLI's anonymous flow now points at a real URL.
4. **Real ORCID OAuth code exchange.** Server-side httpx call to orcid.org behind a config check. Tests mock the httpx call; dev-mode path stays for the default `rrxiv serve`.
5. **Real hCaptcha siteverify.** Same pattern.

### Phase 2 — close API surface (Tier 2)

6. **`POST /submissions` + multipart upload.** New `submissions/` domain module. Filesystem source storage.
7. **`GET /papers/{id}/source`.** Streams from filesystem.
8. **`GET /papers/{id}/versions`.** Walks `previous_version` chain.
9. **`GET /search/papers`, `GET /search/claims`.** Simple substring match.
10. **`POST /snapshots`.** Builds tar.gz, persists, returns manifest.

### Phase 3 — strictness + conformance (Tier 3)

11. **Strict annotation validation.** `rrxiv.models.Annotation` validation in the route. Per-type payload errors surface cleanly.
12. **Cross-paper claim ID validation.** Reject malformed claim ids; check existence.
13. **End-to-end conformance fixture.** Single test exercising enroll → submit → search → annotate → snapshot.

### Phase 4 — RRP open questions (Tier 4)

14. **Refresh tokens — RRP-0009 + impl.** Server route + client method + CLI behaviour. New RRP doc in companion repo.
15. **Agent key rotation — RRP-0010 + impl.** Server route + client method + CLI subcommand. RRP doc.
16. **SQLite persistent store — RRP-0011 + impl.** New `store/sqlite.py`. Settings flag. RRP doc.

### Phase 5 — stretch

17. **Device flow — RRP-0012 + impl.** If scope still tractable.
18. **`rrxiv.testing.live_server` fixture.**
19. **`rrxiv conformance` CLI subcommand.**

### Phase 6 — wrap

20. **CHANGELOG + MIGRATIONS pass.** Document everything new. Update PR #21 description.
21. **Companion RRP PR (rrxiv repo) update.** Add 0009/0010/0011/0012 to the existing PR or as a follow-up.

## Testing strategy

- **Per-feature unit tests** for each new module and route.
- **Cross-tests extended.** `tests/test_server_cross.py` grows to cover every new endpoint.
- **End-to-end conformance fixture** as the headline test.
- **SQLite tests** exercise the same `Store` interface tests as `MemoryStore` — single test file parameterised across both backends.
- **Real-IdP mock tests.** httpx.MockTransport to simulate orcid.org and hcaptcha.com responses.

Targeting **~80 new tests** on top of the current 285. Keep total runtime under 15 seconds.

## Risks

| Risk | Mitigation |
|---|---|
| FastAPI multipart ergonomics may surprise. | Use FastAPI's `UploadFile` + `Form()` together; well-trodden path. |
| SQLite migration drift between dev and tests. | Single `init_schema()` function; tests use `:memory:` SQLite. |
| Mock-based ORCID tests can drift from reality. | Document the *real* request shape in the test; pin to ORCID's documented endpoints. |
| Phase 4-5 scope balloons; we miss device flow. | Device flow drops cleanly — RRP-0012 + impl can land in a follow-up. |
| End-to-end conformance test becomes flaky. | Run uvicorn on a randomly-bound port, retry connect probe loop, set strict timeouts. |

## Done = ready to ship the mega-PR

- All 285 existing tests still pass.
- ~360 total tests, lint + mypy clean.
- `rrxiv login orcid --no-browser` actually works against `rrxiv serve` (no test pre-seeding).
- `rrxiv login anonymous` actually works against `rrxiv serve` (renders hCaptcha widget).
- `rrxiv submit <paper.tex>` (or equivalent) puts a paper into the server.
- `rrxiv search papers <query>` returns matches.
- `rrxiv agent rotate-key --handle @bot` rotates the key.
- `rrxiv serve --store sqlite:///./rrxiv.db` persists across restarts.
- CHANGELOG + RRPs 0009/0010/0011 (and maybe 0012) merged.

## References

- [RFC 6749 §4.1](https://datatracker.ietf.org/doc/html/rfc6749#section-4.1) — OAuth 2.0 authorization code flow (the dance ORCID implements)
- [RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628) — OAuth 2.0 device authorization grant
- [hCaptcha siteverify docs](https://docs.hcaptcha.com/#verify-the-user-response-server-side)
- [ORCID public API tutorial](https://info.orcid.org/documentation/api-tutorials/api-tutorial-get-and-authenticated-orcid-id/)
- The first-half plan: [`plan-cli-sigs-server.md`](plan-cli-sigs-server.md)
