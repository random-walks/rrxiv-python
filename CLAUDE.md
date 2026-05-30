# rrxiv-python — parser + SDK + reference server + CLI

Complements the workspace rules in `../../CLAUDE.md`. This is the Python side of
the protocol: the TeX→CIR parser, the client SDK, the FastAPI **reference
server** (ground truth for API behaviour), and the `rrxiv` CLI.

## Toolchain (use `uv`, never `pip`)

- `uv run pytest` · `uv run ruff check .` · `uv run mypy src` (strict). Python 3.11+.
- All four must pass before a PR: ruff, mypy, pytest, and the OpenAPI drift-gate `uv run python scripts/dump_openapi.py --check`.

## Structure

- `src/rrxiv/parser/` — TeX + sidecar → CIR (`build.py`).
- `src/rrxiv/client/` — SDK (incl. `AgentSigningKey`, RFC-9421 signing).
- `src/rrxiv/server/` — FastAPI reference server. Its generated OpenAPI is authoritative over the prose in `rrxiv/spec` / `schema/api.openapi.yaml`.
- `src/rrxiv/cli/` — the `rrxiv` CLI (`login`, `auth`, `submit`, `seed-store`, …).
- `src/rrxiv/_schemas/` — schemas **vendored** from `../rrxiv/schema` (byte-for-byte). `src/rrxiv/models/_generated/` — pydantic models from those schemas.

## Schema sync (don't hand-edit generated files)

Edit canonical schemas in `../rrxiv/schema`, then here: `scripts/sync_schemas.sh` → `scripts/regen_models.sh` → tests. Revert timestamp-only churn in generated files; commit only meaningful diffs.

## Identifier model gotcha (RRP-0029)

`paper.id` is a server-minted **UUIDv7** (the storage PK; `server/ids.py:uuid7`). But **claims and annotations are keyed off `id_slug`, NOT `paper.id`** — `claim.id` is `<id_slug>:<local_label>` and `claim.paper_id` is the `id_slug`. Every "claims/annotations for this paper" filter uses `claim_owner_key(paper)` (= `id_slug or id`), while blob/PK lookups (`get_paper`/`load_source`/…) use `paper.id`. Claim-prefix canonicalisation lives in `server/papers/claim_ids.py` and runs on **both** ingest paths (seed-store + `POST /submissions`). There's a regression test pinning slug-keyed resolution + cross-paper edge survival — keep it green when touching claim/annotation queries.
