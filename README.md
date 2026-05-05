# rrvix-python

Reference Python client for the [rrvix protocol](https://github.com/<org>/rrvix).

Status: **v0.1 in active development.** Not yet on PyPI.

## Quickstart (when ready)

```bash
pip install rrvix
rrvix parse path/to/paper.tex --output paper.cir.json
rrvix validate paper.cir.json
```

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run mypy src/
```

See `BOOTSTRAP.md` for the full Phase 0 milestone list.
