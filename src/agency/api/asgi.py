"""Module-level ASGI entrypoint, for tools that need an import string.

    uvicorn agency.api.asgi:app
    gunicorn -k uvicorn.workers.UvicornWorker agency.api.asgi:app

`create_app` is still the real interface -- this is a thin adapter for `--reload` and
for process managers, both of which re-import a module in a fresh process and so cannot
be handed an app object. Anything that can call a function should call `create_app`.
"""

from __future__ import annotations

from agency.api.app import create_app

app = create_app()
