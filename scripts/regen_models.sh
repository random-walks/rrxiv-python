#!/usr/bin/env bash
# regen_models.sh — regenerate pydantic models from the synced schemas.
#
# Reads src/rrxiv/_schemas/*.schema.json and emits pydantic v2 BaseModel
# classes into src/rrxiv/models/_generated/. The generator is
# datamodel-code-generator, declared as a dev dependency in pyproject.toml.
#
# Usage:
#   ./scripts/regen_models.sh
#
# Run scripts/sync_schemas.sh first to populate src/rrxiv/_schemas/.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCHEMAS="src/rrxiv/_schemas"
OUT="src/rrxiv/models/_generated"

if [[ ! -d "$SCHEMAS" ]] || ! ls "$SCHEMAS"/*.schema.json >/dev/null 2>&1; then
    echo "ERROR: no schemas in $SCHEMAS. Run scripts/sync_schemas.sh first." >&2
    exit 1
fi

echo "==> Schemas: $SCHEMAS"
echo "==> Out:     $OUT"

# Wipe and recreate the output directory
rm -rf "$OUT"
mkdir -p "$OUT"

# Generate. One Python module per input schema.
uv run --with datamodel-code-generator \
    datamodel-codegen \
    --input "$SCHEMAS" \
    --output "$OUT" \
    --input-file-type jsonschema \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.11 \
    --use-standard-collections \
    --use-union-operator \
    --use-double-quotes \
    --use-field-description \
    --use-schema-description \
    --use-default \
    --field-constraints

# Make the dir a package
touch "$OUT/__init__.py"

# Tidy up
uv run ruff check --fix "$OUT" >/dev/null 2>&1 || true
uv run ruff format "$OUT" >/dev/null

echo ""
echo "==> Generated:"
find "$OUT" -name '*.py' -not -name '__init__.py' | sed 's/^/  /'

echo ""
echo "==> Next: review src/rrxiv/models/__init__.py to re-export the generated classes,"
echo "    then run 'uv run pytest' to confirm nothing broke."
