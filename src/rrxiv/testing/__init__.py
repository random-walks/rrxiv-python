"""Testing utilities for rrxiv-python consumers.

The most useful piece is :class:`MockRrxivServer`, an in-process
``httpx.MockTransport`` factory that implements just enough of the
rrxiv API to let library tests exercise the client end-to-end without
a real server.

Usage::

    from rrxiv.client import RrxivClient
    from rrxiv.testing import MockRrxivServer

    server = MockRrxivServer()
    server.add_paper({"id": "p1", "title": "Test", ...})

    client = RrxivClient("https://example.test/api/v0", transport=server.transport)
    paper = client.get_paper("p1")
    assert paper.id == "p1"

This module is in the package's main code (not the test tree) so it's
importable by downstream consumers' tests too.
"""

from rrxiv.testing.mock_server import MockRrxivServer

# `live_server` is the pytest fixture for downstream tests that
# want a real uvicorn-backed reference server. Imported lazily so
# clients without `[server]` extra installed don't choke at import.
try:
    from rrxiv.testing.live_server import LiveServer, live_server
except ImportError:  # pragma: no cover
    LiveServer = None  # type: ignore[assignment, misc]
    live_server = None  # type: ignore[assignment]


__all__ = ["LiveServer", "MockRrxivServer", "live_server"]
