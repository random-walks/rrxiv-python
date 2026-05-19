# rrxiv reference server — production image
#
# Builds a single-process FastAPI app behind uvicorn, with SQLite as the
# persistent store. The seed corpus is baked in at build time; first boot
# initialises the database from /seed/ if it's empty.
#
# Target: Fly.io with a persistent volume mounted at /data.
# Env vars set in fly.toml:
#   RRXIV_STORE_URL=sqlite:////data/rrxiv.db
#   RRXIV_API_BASE=https://api.rrxiv.org/api/v0
#   RRXIV_DEV_MODE=0
#   RRXIV_CORS_ORIGINS=https://rrxiv.org,https://www.rrxiv.org

FROM python:3.12-slim

WORKDIR /app

# System deps — minimal. Python 3.12 includes sqlite3.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy project metadata first for better Docker layer caching.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install with the server extra. Use pip rather than uv because the
# Fly.io builder expects a self-contained image without external tools.
RUN pip install --no-cache-dir ".[server]"

# Bake the seed corpus into the image.
COPY seed/ /seed/

# Entrypoint script handles first-boot seeding then exec's uvicorn.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# /data is the persistent volume mount point on Fly. The entrypoint
# initialises /data/rrxiv.db from /seed/ on first boot.
VOLUME ["/data"]

EXPOSE 8080

# Healthcheck — Fly's [checks] block will hit this independently, but
# the Docker HEALTHCHECK is useful for local testing.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:8080/api/v0/version').read()" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
