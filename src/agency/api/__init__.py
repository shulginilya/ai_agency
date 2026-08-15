"""HTTP surface for the harness.

Deliberately import-light: `agency.api.app` pulls in FastAPI, which is an optional
extra (`pip install -e ".[api]"`). Import `create_app` from `agency.api.app` at the
point of use so `agency run` keeps working without the web dependencies installed.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args: object, **kwargs: object) -> object:
    """Lazy re-export so `import agency.api` does not require FastAPI."""
    from agency.api.app import create_app as _create_app

    return _create_app(*args, **kwargs)  # type: ignore[arg-type]
