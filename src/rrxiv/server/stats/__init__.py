"""Stats package — community pulse + corpus aggregates.

The legacy ``GET /stats`` (in ``discovery.router``) stays where it
is for backward compatibility; this package owns the new
``GET /stats/pulse`` endpoint and its pure computation helpers.
"""

from rrxiv.server.stats.pulse import PulseWindow, compute_pulse, parse_window

__all__ = ["PulseWindow", "compute_pulse", "parse_window"]
