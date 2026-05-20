# rrxiv-python

Reference Python implementation of the [rrxiv protocol](https://github.com/random-walks/rrxiv) — parser, client SDK, FastAPI reference server, conformance test suite, and CLI.

**Status: v0.1 — running in production at [api.rrxiv.com](https://api.rrxiv.com/api/v0/docs).** Not yet on PyPI; install from the git ref.

## Installation

```bash
# Library + parser only
pip install "rrxiv @ git+https://github.com/random-walks/rrxiv-python.git@main"

# Library + server (FastAPI, uvicorn, ed25519 signatures, sentry, etc.)
pip install "rrxiv[server] @ git+https://github.com/random-walks/rrxiv-python.git@main"
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add "rrxiv[server] @ git+https://github.com/random-walks/rrxiv-python.git@main"
```

## Quick tour

### Parse a paper

```bash
rrxiv parse paper/main.tex --output paper.cir.json
rrxiv validate paper.cir.json
```

Produces a [Canonical Intermediate Representation](https://github.com/random-walks/rrxiv/blob/main/schema/cir.schema.json) — paper metadata + claims + annotations + claim-graph edges, validated against the JSON Schema in `random-walks/rrxiv`.

### Run a local instance

```bash
# Memory store (lost on exit) — fastest path to play with the API
rrxiv serve --dev-mode

# Persistent SQLite store with a seed corpus
rrxiv serve \
  --store sqlite:////tmp/rrxiv.db \
  --seed-dir ./seed \
  --port 8765
```

`http://127.0.0.1:8765/api/v0/docs` then shows the full OpenAPI; `/api/v0/papers` lists the seeded corpus.

### Bulk-load a corpus

```bash
# First boot: seed a fresh DB from a directory of *.cir.json + *.pdf
# + *.source.tar.gz triples
rrxiv seed-store --from ./seed --store sqlite:////data/rrxiv.db

# When claim ids change between releases, --reset wipes the corpus tables
# before re-seeding so no orphan rows linger
rrxiv seed-store --from ./seed --store sqlite:////data/rrxiv.db --reset
```

`seed-store` also stamps `paper.source.uri` / `paper.source.rendered_pdf_uri` with the canonical `/api/v0/papers/{id}/{source,pdf}` endpoints so the web client can resolve them.

## Package layout

- `rrxiv.models` — Pydantic v2 models generated from the JSON Schemas (`Paper`, `Claim`, `Annotation`, `Citation`, `CIR`, plus enums).
- `rrxiv.parser` — `.tex` → CIR. Recognises the [`rrxiv.cls`](https://github.com/random-walks/rrxiv/blob/main/template/rrxiv.cls) `\claim` / `\evidence` / `\dependson` markup.
- `rrxiv.client` — async + sync HTTP client + retry policy + signature middleware.
- `rrxiv.server` — FastAPI app factory + 8 routers (auth, papers, claims, annotations, snapshots, search, submissions, sources). Pluggable `Store` (memory / sqlite / future Postgres).
- `rrxiv.testing` — `live_server` pytest fixture for running the server against real HTTP.
- `rrxiv.cli` — Typer CLI: `parse`, `validate`, `serve`, `seed-store`, `snapshot`, `login`, `submit`, `doctor`.
- `tests/` — 397 unit tests + the live cross-conformance test (`test_server_cross.py`) that runs the protocol-level test suite against the in-process server.

## Development

```bash
uv sync --all-extras
uv run pytest               # 397 passed, 4 skipped
uv run ruff check .
uv run mypy src/
```

## Reference server in production

The Fly.io deployment that powers `api.rrxiv.com` lives in a separate private repo, [`rrxiv-instance`](https://github.com/random-walks/rrxiv-instance) — it pins a specific `rrxiv-python` commit, layers on the production Dockerfile + Fly config, and bakes the canonical 9-paper seed corpus into the image. The split is intentional: `rrxiv-python` stays a library anyone can fork to spin up their own instance; the canonical instance's operational concerns (CORS allowlist, ORCID redirect URIs, Sentry DSNs) belong to the overlay.

If you want to run your own rrxiv instance, fork that repo's structure — don't depend on its config.

## Schema sync

Schemas live canonically in [random-walks/rrxiv](https://github.com/random-walks/rrxiv). We vendor them under `src/rrxiv/_schemas/` and regenerate Pydantic models from them.

When the canonical schemas change (a new field, a tightened enum, a new schema file), run:

```bash
./scripts/sync_schemas.sh                 # default: ../rrxiv/schema   (workspace pattern)
./scripts/sync_schemas.sh /path/to/schema # explicit path

./scripts/regen_models.sh                 # regenerate src/rrxiv/models/_generated/
```

`sync_schemas.sh` writes `src/rrxiv/_schemas_manifest.txt` with the source path, git SHA, branch, and timestamp so you always know which version of the protocol the vendored schemas correspond to.

`regen_models.sh` uses [datamodel-code-generator](https://github.com/koxudaxi/datamodel-code-generator) (a dev dep, declared in `pyproject.toml`) to emit one Pydantic v2 module per schema into `src/rrxiv/models/_generated/`. The hand-written `src/rrxiv/models/__init__.py` re-exports the public surface so `from rrxiv.models import Paper, Claim, CIR, ...` keeps working when the generator output rearranges itself.

The cross-test in [`tests/test_models.py`](tests/test_models.py) loads every fixture from `../rrxiv/tests/schemas/fixtures/` and checks that pydantic agrees with ajv on each. If the two diverge (a fixture passes ajv but fails pydantic, or vice versa), CI fails — that catches both codegen bugs and silent schema drift.

## License

MIT (code) — see `LICENSE`. The protocol spec + schemas in `random-walks/rrxiv` are CC-BY-4.0.
