#!/usr/bin/env bash
# sync_schemas.sh — copy rrxiv JSON Schemas into this repo.
#
# rrxiv-python vendors the schemas from the canonical rrxiv repo. This
# script copies them in and writes a MANIFEST recording the source path,
# git SHA, and timestamp for traceability.
#
# Usage:
#   ./scripts/sync_schemas.sh                              # default: ../rrxiv/schema
#   ./scripts/sync_schemas.sh /path/to/rrxiv/schema        # explicit path
#
# After syncing, run scripts/regen_models.sh to regenerate pydantic models.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-$ROOT/../rrxiv/schema}"
DEST="$ROOT/src/rrxiv/_schemas"
MANIFEST="$ROOT/src/rrxiv/_schemas_manifest.txt"

if [[ ! -d "$SOURCE" ]]; then
    echo "ERROR: schema source directory not found: $SOURCE" >&2
    echo "Pass an explicit path: $0 /path/to/rrxiv/schema" >&2
    exit 1
fi

echo "==> Source: $SOURCE"
echo "==> Dest:   $DEST"

mkdir -p "$DEST"

# Wipe any stale schemas (but keep MANIFEST and __init__.py)
find "$DEST" -maxdepth 1 -name '*.schema.json' -delete

# Copy fresh
shopt -s nullglob
schemas=("$SOURCE"/*.schema.json)
if [[ ${#schemas[@]} -eq 0 ]]; then
    echo "ERROR: no *.schema.json files found in $SOURCE" >&2
    exit 1
fi

for f in "${schemas[@]}"; do
    cp "$f" "$DEST/"
    echo "  + $(basename "$f")"
done

# Make the dir an importable package
touch "$DEST/__init__.py"

# Record provenance
SOURCE_REPO="$(cd "$SOURCE" && git rev-parse --show-toplevel 2>/dev/null || echo "")"
SOURCE_SHA=""
SOURCE_BRANCH=""
SOURCE_DIRTY=""
if [[ -n "$SOURCE_REPO" ]]; then
    SOURCE_SHA="$(git -C "$SOURCE_REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
    SOURCE_BRANCH="$(git -C "$SOURCE_REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    if ! git -C "$SOURCE_REPO" diff --quiet 2>/dev/null; then
        SOURCE_DIRTY=" (dirty working tree)"
    fi
fi

cat > "$MANIFEST" <<EOF
# Schema sync manifest

Synced from: $SOURCE
Source repo: ${SOURCE_REPO:-(not a git repo)}
Branch:      ${SOURCE_BRANCH:-unknown}
Commit:      ${SOURCE_SHA:-unknown}${SOURCE_DIRTY}
Synced at:   $(date -u +"%Y-%m-%dT%H:%M:%SZ")

Files:
$(cd "$DEST" && ls *.schema.json | sed 's/^/  - /')

Run scripts/regen_models.sh to regenerate pydantic models from these.
EOF

echo ""
echo "==> Wrote $MANIFEST"
echo ""
cat "$MANIFEST"
echo ""
echo "==> Next: ./scripts/regen_models.sh"
