#!/usr/bin/env bash
# entrypoint.sh — first-boot seed + uvicorn launch.
#
# On every container start:
#   1. If /data/rrxiv.db doesn't exist, seed it from /seed/.
#   2. Exec uvicorn against the FastAPI factory.
#
# Idempotent: subsequent restarts skip the seed step and just start
# the server against the existing database.

set -euo pipefail

STORE_URL="${RRXIV_STORE_URL:-sqlite:////data/rrxiv.db}"

# Strip the sqlite:/// prefix to get the filesystem path (4 slashes
# meaning absolute path on POSIX).
DB_PATH="${STORE_URL#sqlite:///}"
if [[ "$DB_PATH" == "$STORE_URL" ]]; then
    DB_PATH=""  # Not a sqlite URL; nothing to seed
fi

if [[ -n "$DB_PATH" && ! -f "$DB_PATH" ]]; then
    echo "==> First boot — seeding $DB_PATH from /seed/"
    # Ensure the parent dir exists (fly's volume mount may be fresh).
    mkdir -p "$(dirname "$DB_PATH")"
    rrxiv seed-store --from /seed/ --store "$STORE_URL"
else
    echo "==> Existing database at $DB_PATH — skipping seed."
fi

echo "==> Starting uvicorn on 0.0.0.0:8080"
exec uvicorn 'rrxiv.server.app:build_app' \
    --factory \
    --host 0.0.0.0 \
    --port 8080 \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --log-level "${RRXIV_LOG_LEVEL:-info}"
