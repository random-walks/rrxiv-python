#!/usr/bin/env python3
"""Generate the canonical rrxiv OpenAPI document from the reference server.

The FastAPI reference server (``rrxiv.server.build_app``) is the single
source of truth for the HTTP API surface. This script dumps its generated
OpenAPI 3.1 document to ``openapi.yaml`` at the repo root, applying a small
*publishing* transform (canonical ``servers``, spec-facing ``info``) so the
artifact reads as the protocol's published API rather than the dev server's
runtime self-description.

The committed ``openapi.yaml`` is what ``rrxiv/schema/api.openapi.yaml`` and
the web client's API reference are synced from. CI re-runs this script with
``--check`` and fails if the committed file is stale, so the documented API
can never silently drift from the implemented one.

Usage:
    python scripts/dump_openapi.py            # write openapi.yaml
    python scripts/dump_openapi.py --check    # exit 1 if openapi.yaml is stale
    python scripts/dump_openapi.py --stdout   # print to stdout, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from rrxiv.server import build_app

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "openapi.yaml"

# The canonical hosted instance. The live reference server intentionally
# declares no `servers` (so its own /docs work against whatever host serves
# it, dev or prod); the *published* spec points at the canonical API host.
CANONICAL_SERVER = "https://api.rrxiv.com"


def generate_spec() -> dict:
    """Build the app, generate its OpenAPI, apply the publishing transform."""
    app = build_app()
    spec = app.openapi()

    spec["servers"] = [
        {"url": CANONICAL_SERVER, "description": "Canonical rrxiv instance"},
    ]

    info = spec.setdefault("info", {})
    info["title"] = "rrxiv HTTP API"
    info["description"] = (
        "The HTTP API of the rrxiv protocol.\n\n"
        "**This document is generated** from the reference server "
        "(`rrxiv-python`) — it is the source of truth for the API surface; "
        "do not edit it by hand. See `spec/0007-api.md` for prose design "
        "rationale.\n\n"
        "Request/response schemas are inlined under `components/schemas`. "
        "Conforming servers MUST validate payloads against the canonical "
        "JSON Schemas in `rrxiv/schema/`."
    )
    info["license"] = {"name": "MIT (code), CC-BY-4.0 (spec)"}

    return spec


def render_yaml(spec: dict) -> str:
    # sort_keys=False preserves FastAPI's deterministic insertion order
    # (paths in router-registration order), which keeps diffs readable.
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed openapi.yaml is out of date",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print the spec to stdout instead of writing the file",
    )
    args = parser.parse_args()

    spec = generate_spec()
    rendered = render_yaml(spec)

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            sys.stderr.write(
                f"ERROR: {OUTPUT.name} is out of date with the reference "
                "server's OpenAPI.\n"
                "Regenerate it:  python scripts/dump_openapi.py\n"
            )
            return 1
        print(f"{OUTPUT.name} is up to date.")
        return 0

    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(spec.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
