"""The Scalar API reference page, served at `/reference`.

A second UI over the *same* `/openapi.json` that `/docs` and `/redoc` already render --
there is nothing here to keep in sync with the routes, and deleting this module leaves
the API unchanged. It exists because Swagger UI is good at poking one endpoint and poor
at the two things people actually do with this API: reading the session lifecycle end to
end, and lifting a working request into curl or python.

Why not the `scalar-fastapi` package: it is this template plus a pydantic config surface,
and its defaults are the ones we would have to override anyway -- an unpinned CDN URL
(`@scalar/api-reference`, i.e. whatever was published this morning), `telemetry=True`,
and a favicon pointing at fastapi.tiangolo.com. A dependency whose whole contribution is
forty lines of HTML is not worth the supply-chain surface, and pinning the bundle here
means the docs page cannot change under a running deployment.

The bundle still comes from a CDN, so this page is blank in an airgapped container --
same caveat as `/docs`, which loads Swagger UI the same way. `AGENCY_API_DOCS_JS_URL`
is the escape hatch: point it at a self-hosted copy of the same file.
"""

from __future__ import annotations

import json
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from agency.config import Settings

REFERENCE_PATH = "/reference"

SCALAR_VERSION = "1.67.0"
"""Pinned, deliberately. An unversioned CDN URL makes every page load a fetch of code
nobody in this repo has seen, and turns "the docs page went blank" into an incident with
no local change to bisect."""

SCALAR_JS_URL = f"https://cdn.jsdelivr.net/npm/@scalar/api-reference@{SCALAR_VERSION}"

_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>%(title)s</title>
    <style>
      body { margin: 0; }
    </style>
  </head>
  <body>
    <div id="app"></div>
    <script src="%(js_url)s"></script>
    <script>
      Scalar.createApiReference("#app", %(config)s)
    </script>
  </body>
</html>
"""


def reference_html(*, openapi_url: str, title: str, js_url: str) -> str:
    """The page. Pure function of its arguments, so a test can assert on it directly."""
    config = {
        "url": openapi_url,
        # The three defaults that phone home or advertise, all off. This page is served
        # by the API it documents and describes a private surface: the endpoint names
        # alone say more about a deployment than anyone meant to publish, and "Ask AI"
        # would ship the spec -- and whatever someone types about it -- to a third party.
        "telemetry": False,
        "agent": {"disabled": True},
        # Scalar's Configure/Share/Deploy toolbar, which is a funnel to their hosted
        # product. It only renders on localhost by default, i.e. exactly where this
        # repo's developers would see it.
        "showDeveloperTools": False,
        # Not persisted: the key would sit in this browser's storage for anyone with the
        # machine. Paste it per session, as Swagger UI already makes you.
        "persistAuth": False,
    }
    return _TEMPLATE % {
        "title": escape(title),
        # Both interpolated into HTML: the URL into an attribute, the config into a
        # script body. `json.dumps` handles the second; the URL is escaped for the first.
        "js_url": escape(js_url, quote=True),
        "config": json.dumps(config),
    }


def install_reference(app: FastAPI, settings: Settings) -> None:
    """Register `GET /reference`, or nothing when the docs are switched off.

    Gated by the same `AGENCY_API_DOCS_ENABLED` as `/docs`: a deployment that hides its
    endpoint map should not keep one door open. `include_in_schema=False` for the same
    reason FastAPI hides its own docs routes -- a UI is not part of the API contract, and
    a generated client should not grow a `get_reference()`.
    """
    openapi_url = app.openapi_url
    if not settings.api_docs_enabled or not openapi_url:
        return

    js_url = settings.api_docs_js_url.strip() or SCALAR_JS_URL
    title = f"{settings.api_title} API reference"

    @app.get(REFERENCE_PATH, include_in_schema=False, response_class=HTMLResponse)
    async def reference() -> HTMLResponse:
        return HTMLResponse(reference_html(openapi_url=openapi_url, title=title, js_url=js_url))
