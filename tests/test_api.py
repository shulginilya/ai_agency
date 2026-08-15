"""HTTP contract tests.

Every test drives the real ASGI app through httpx's ASGI transport -- real routing,
real middleware, real error handlers -- with the `Runner` dependency replaced by a fake.
No socket is bound, no model session starts, and the suite costs nothing, which is the
whole point of the seam in `deps.py`.

What is asserted here is the contract: status codes, the problem+json shape, auth,
admission control, and the record -> response mapping. Not the agent's behaviour --
that belongs in `evals/`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from agency.api.app import create_app
from agency.api.deps import get_runner
from agency.api.errors import CONTENT_TYPE
from agency.config import Profile, Settings
from agency.obs.cost import RunRecord, ToolCall


def _finished_record(**overrides: Any) -> RunRecord:
    record = RunRecord(run_id="rec00001", prompt="hi", profile="sandboxed", model="test-model")
    record.output = "done"
    record.num_turns = 2
    record.duration_ms = 1234
    record.total_cost_usd = 0.005
    record.record_tool(ToolCall(tool="Read", phase="post"))
    record.record_tool(ToolCall(tool="Agent", phase="post", detail={"agent_type": "reviewer"}))
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


class FakeRunner:
    """Stands in for `runner.run_once`. Records what the route asked for."""

    def __init__(self, record: RunRecord | None = None, delay: float = 0.0) -> None:
        self.record = record or _finished_record()
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, prompt: str, *, profile: Profile | None = None, team: bool = False
    ) -> RunRecord:
        self.calls.append({"prompt": prompt, "profile": profile, "team": team})
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.record


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"api_run_timeout_s": 5.0, "api_docs_enabled": True})


def make_client(settings: Settings, runner: Any) -> httpx.AsyncClient:
    app = create_app(settings, runner=runner)
    # dependency_overrides as well as the constructor argument: the constructor covers
    # production wiring, the override proves the seam works for anyone extending this.
    app.dependency_overrides[get_runner] = lambda: runner
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def client(api_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    runner = FakeRunner()
    async with make_client(api_settings, runner) as c:
        c.runner = runner  # type: ignore[attr-defined]
        yield c


# --- health --------------------------------------------------------------------------


async def test_healthz_is_cheap_and_unauthenticated(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readyz_reports_writable_dirs_and_capacity(client: httpx.AsyncClient) -> None:
    body = (await client.get("/readyz")).json()
    assert body["status"] == "ready"
    assert body["checks"]["workspace"] == "ok"
    assert body["capacity"]["in_flight"] == 0


async def test_health_endpoints_stay_open_when_auth_is_on(api_settings: Settings) -> None:
    secured = api_settings.model_copy(update={"api_key": "s3cret"})
    async with make_client(secured, FakeRunner()) as c:
        assert (await c.get("/healthz")).status_code == 200


# --- the demo endpoint ---------------------------------------------------------------


async def test_run_returns_the_mapped_record(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/runs", json={"prompt": "count to three"})
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "rec00001"
    assert body["output"] == "done"
    assert body["tools_used"] == ["Read", "Agent"]
    assert body["delegated_to"] == ["reviewer"]
    assert body["total_cost_usd"] == 0.005


async def test_run_forwards_profile_and_team_to_the_runner(client: httpx.AsyncClient) -> None:
    await client.post("/v1/runs", json={"prompt": "x", "profile": "eval", "team": True})
    call = client.runner.calls[-1]  # type: ignore[attr-defined]
    assert call == {"prompt": "x", "profile": "eval", "team": True}


async def test_an_errored_agent_is_still_a_200(api_settings: Settings) -> None:
    # The request succeeded; the agent failed. Collapsing these into a 5xx would leave
    # a client unable to tell "the model failed" from "we could not run the model".
    runner = FakeRunner(_finished_record(is_error=True, output="I could not do that"))
    async with make_client(api_settings, runner) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"})
    assert response.status_code == 200
    assert response.json()["is_error"] is True


async def test_every_response_carries_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/runs", json={"prompt": "x"})
    assert response.headers["x-request-id"]


async def test_an_inbound_request_id_is_preserved(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"x-request-id": "trace-abc"})
    assert response.headers["x-request-id"] == "trace-abc"


# --- validation ----------------------------------------------------------------------


async def test_missing_prompt_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/runs", json={})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(CONTENT_TYPE)
    body = response.json()
    assert body["status"] == 422
    assert body["type"].endswith("/validation-failed")
    assert body["errors"]


async def test_empty_prompt_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/runs", json={"prompt": "   x"})).status_code == 200
    assert (await client.post("/v1/runs", json={"prompt": ""})).status_code == 422


async def test_unknown_field_is_rejected_rather_than_ignored(client: httpx.AsyncClient) -> None:
    # A silently dropped option is the worst failure mode here: the caller believes it
    # pinned the profile and quietly gets the server default.
    response = await client.post("/v1/runs", json={"prompt": "x", "prof1le": "eval"})
    assert response.status_code == 422


async def test_unknown_profile_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/runs", json={"prompt": "x", "profile": "yolo"})
    assert response.status_code == 422


async def test_unknown_route_is_also_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(CONTENT_TYPE)
    assert response.json()["status"] == 404


# --- auth ----------------------------------------------------------------------------


async def test_no_key_configured_means_open(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/runs", json={"prompt": "x"})).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer wrong"},
    ],
)
async def test_bad_or_missing_key_is_401(api_settings: Settings, headers: dict[str, str]) -> None:
    secured = api_settings.model_copy(update={"api_key": "s3cret"})
    async with make_client(secured, FakeRunner()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["type"].endswith("/unauthorized")


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "s3cret"},
        {"Authorization": "Bearer s3cret"},
    ],
)
async def test_either_scheme_authenticates(api_settings: Settings, headers: dict[str, str]) -> None:
    secured = api_settings.model_copy(update={"api_key": "s3cret"})
    async with make_client(secured, FakeRunner()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers=headers)
    assert response.status_code == 200


# --- admission control and timeouts --------------------------------------------------


async def test_a_saturated_pool_refuses_with_503(api_settings: Settings) -> None:
    bounded = api_settings.model_copy(update={"api_max_concurrent_runs": 1})
    runner = FakeRunner(delay=0.2)
    async with make_client(bounded, runner) as c:
        first = asyncio.create_task(c.post("/v1/runs", json={"prompt": "slow"}))
        await asyncio.sleep(0.05)  # let the first request take the only slot
        second = await c.post("/v1/runs", json={"prompt": "queued"})
        assert (await first).status_code == 200

    assert second.status_code == 503
    assert second.headers["retry-after"] == "5"
    assert second.json()["type"].endswith("/overloaded")


async def test_slots_are_released_after_a_failure(api_settings: Settings) -> None:
    class Boom(FakeRunner):
        async def __call__(self, prompt: str, **kwargs: Any) -> RunRecord:
            raise RuntimeError("transport died")

    bounded = api_settings.model_copy(update={"api_max_concurrent_runs": 1})
    async with make_client(bounded, Boom()) as c:
        assert (await c.post("/v1/runs", json={"prompt": "x"})).status_code == 502
        # A leaked slot would make this one 503 instead.
        assert (await c.post("/v1/runs", json={"prompt": "x"})).status_code == 502


async def test_a_runner_exception_becomes_502_without_leaking_internals(
    api_settings: Settings,
) -> None:
    class Boom(FakeRunner):
        async def __call__(self, prompt: str, **kwargs: Any) -> RunRecord:
            raise RuntimeError("ANTHROPIC_API_KEY=sk-secret is invalid")

    async with make_client(api_settings, Boom()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"})
    assert response.status_code == 502
    assert "sk-secret" not in response.text
    assert "RuntimeError" in response.json()["detail"]


async def test_an_overlong_run_is_cancelled_and_returns_504(api_settings: Settings) -> None:
    impatient = api_settings.model_copy(update={"api_run_timeout_s": 0.05})
    async with make_client(impatient, FakeRunner(delay=5.0)) as c:
        response = await c.post("/v1/runs", json={"prompt": "slow"})
    assert response.status_code == 504
    assert response.json()["type"].endswith("/run-timeout")


# --- openapi -------------------------------------------------------------------------


async def test_openapi_documents_the_endpoint(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert "/v1/runs" in schema["paths"]
    assert "503" in schema["paths"]["/v1/runs"]["post"]["responses"]


async def test_docs_can_be_switched_off(api_settings: Settings) -> None:
    quiet = api_settings.model_copy(update={"api_docs_enabled": False})
    async with make_client(quiet, FakeRunner()) as c:
        assert (await c.get("/openapi.json")).status_code == 404
        assert (await c.get("/docs")).status_code == 404
