"""Single source for the version string reported over HTTP.

Reads the installed distribution metadata so the number cannot drift from
`pyproject.toml`, and degrades to "0.0.0+unknown" when running from a source tree that
was never installed (a plain `git clone` plus `pytest`, for instance).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    VERSION = _dist_version("agency")
except PackageNotFoundError:  # pragma: no cover - only hit in uninstalled source trees
    VERSION = "0.0.0+unknown"

API_VERSION = "v1"
"""Route prefix. Bump by adding a router, never by changing this in place."""
