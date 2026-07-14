"""rrxiv — reference Python client for the rrxiv protocol."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed distribution's metadata,
    # which the build backend takes from pyproject.toml `version`.
    # Keeping a literal here drifted (0.1.0 lingered through the 0.2.x
    # releases, so prod's GET /version and the Sentry release tag lied).
    __version__ = version("rrxiv")
except PackageNotFoundError:  # pragma: no cover — running from a bare source tree
    __version__ = "0.0.0.dev0"
