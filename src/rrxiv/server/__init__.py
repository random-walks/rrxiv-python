"""Reference HTTP server for the rrxiv protocol per RRP-0008.

This is a FastAPI implementation of ``rrxiv/schema/api.openapi.yaml``.
It is **not** the canonical instance — that's a downstream concern
with deployment, real ORCID OAuth registration, persistent storage,
etc. The reference server's job is:

1. Conformance target for other-language client implementations.
2. Local development server for ``rrxiv login`` flows.
3. Cross-validation: ``RrxivClient`` driving the reference server
   through ``httpx.ASGITransport`` exercises the protocol end-to-end.

Storage is in-memory only (``rrxiv.server.store.MemoryStore``);
persistent backends are future RRPs.

Usage::

    # programmatic
    from rrxiv.server import build_app
    app = build_app()

    # CLI
    rrxiv serve --port 8000 --dev-mode

The server expects the ``[server]`` extra to be installed.
"""

from __future__ import annotations

from rrxiv.server.app import build_app
from rrxiv.server.settings import ServerSettings

__all__ = ["ServerSettings", "build_app"]
