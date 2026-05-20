# rrxiv reference server — example production image.
#
# Builds a single-process FastAPI app behind uvicorn, with SQLite as the
# persistent store. The bundled `seed/` corpus is baked into the image
# as a starting point; first-boot seeds the database if it's empty.
#
# Target: Fly.io with a persistent volume mounted at /data — but the
# image is portable to any container host.
#
# This Dockerfile is a REFERENCE for parties wanting to run their own
# rrxiv instance. The canonical instance at rrxiv.com is deployed from
# a separate private repo (rrxiv-instance) that extends this image with
# instance-specific seed data, paper-repo ingestion, and deploy config.
# See `.env.example` in this repo for the env vars the server reads;
# set them at deploy time via `fly secrets set …` or your platform's
# equivalent.

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
