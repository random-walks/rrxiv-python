# rrxiv-python

Reference Python client for the [rrxiv protocol](https://github.com/random-walks/rrxiv).

Status: **v0.1 in active development.** Not yet on PyPI.

## Quickstart (when ready)

```bash
pip install rrxiv
rrxiv parse path/to/paper.tex --output paper.cir.json
rrxiv validate paper.cir.json
```

## What's here today

- `rrxiv.models` — Pydantic v2 models for every protocol object (`Paper`, `Claim`, `Annotation`, `Citation`, `CIR`, plus enums and supporting types). Auto-generated from the rrxiv repo's JSON Schemas.
- `rrxiv.cli.app` — stub Typer CLI with `parse` / `validate` subcommands. Real parser comes in M0.3.
- `tests/` — model construction + round-trip + cross-validation against rrxiv's fixture set.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run mypy src/
```

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

See `BOOTSTRAP.md` (in the rrxiv repo) for the full Phase 0 milestone list.
