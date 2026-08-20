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

from agency.agents.definitions import AGENTS
from agency.agents.team import TEAM
from agency.api.app import create_app
from agency.api.deps import get_runner, get_slots
from agency.api.errors import CONTENT_TYPE
from agency.config import Profile, Settings
from agency.obs.agent_state import REGISTRY
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


# --- agent discovery -----------------------------------------------------------------
# Asserted against the live rosters, not a hardcoded list: the point of the endpoint is
# that it cannot drift from what `options.build_options` actually loads, so a test that
# restates the names would pass through exactly the drift it is meant to catch.
#
# The state machine itself is covered in test_agent_state.py; what is asserted here is
# only that the HTTP layer reports it.


@pytest.fixture(autouse=True)
def clean_agent_registry() -> Any:
    # REGISTRY is module-level. Without this, a test that starts an agent leaves every
    # later test reading `running`, and the failures depend on collection order.
    REGISTRY.reset()
    yield
    REGISTRY.reset()


async def test_agents_reports_live_status_over_http(client: httpx.AsyncClient) -> None:
    REGISTRY.record_start("reviewer")
    by_name = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
    assert by_name["reviewer"]["status"] == "running"
    assert by_name["reviewer"]["invocations"] == 1
    assert by_name["critic"]["status"] == "idle"


async def test_updated_at_is_never_null_and_is_stable_for_an_idle_agent(
    client: httpx.AsyncClient,
) -> None:
    async def critic() -> dict[str, Any]:
        agents = (await client.get("/v1/agents")).json()["agents"]
        return dict(next(a for a in agents if a["name"] == "critic"))

    first = await critic()
    assert first["updated_at"] is not None
    # And it does not move just because someone asked again -- an updated_at that
    # advanced per request would read as activity on an agent doing nothing.
    assert (await critic())["updated_at"] == first["updated_at"]
    # `invocations`, not a null timestamp, is what marks it as never having run.
    assert first["invocations"] == 0


async def test_agents_status_follows_a_worker_through_its_lifecycle(
    client: httpx.AsyncClient,
) -> None:
    async def status_of(name: str) -> str:
        agents = (await client.get("/v1/agents")).json()["agents"]
        return str(next(a for a in agents if a["name"] == name)["status"])

    assert await status_of("reviewer") == "idle"
    REGISTRY.record_start("reviewer")
    assert await status_of("reviewer") == "running"
    REGISTRY.record_stop("reviewer")
    assert await status_of("reviewer") == "idle"
    REGISTRY.record_failure("reviewer")
    assert await status_of("reviewer") == "error"


async def test_agents_lists_the_live_rosters(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/agents")
    assert response.status_code == 200
    body = response.json()
    assert [a["name"] for a in body["agents"]] == sorted(AGENTS) + sorted(TEAM)
    assert body["count"] == len(AGENTS) + len(TEAM)


async def test_agents_marks_team_only_workers(client: httpx.AsyncClient) -> None:
    # The distinction a client acts on: a "team" agent is absent unless the run is
    # started with team=true, so listing them flat would be a lie half the time.
    by_name = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
    assert {n: by_name[n]["availability"] for n in AGENTS} == dict.fromkeys(AGENTS, "always")
    assert {n: by_name[n]["availability"] for n in TEAM} == dict.fromkeys(TEAM, "team")


async def test_agents_maps_the_definition_fields(client: httpx.AsyncClient) -> None:
    by_name = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
    entry = by_name["reviewer"]
    assert entry["description"] == TEAM["reviewer"].description
    assert entry["model"] == TEAM["reviewer"].model
    assert entry["tools"] == TEAM["reviewer"].tools
    # camelCase in, snake_case out -- the SDK's naming must not leak into the contract.
    assert entry["max_turns"] == TEAM["reviewer"].maxTurns
    assert "maxTurns" not in entry


async def test_agents_never_exposes_a_system_prompt(client: httpx.AsyncClient) -> None:
    # Prompts are implementation detail. Publishing one makes every prompt tweak a
    # breaking change, and hands an attacker the guardrail wording to work around.
    response = await client.get("/v1/agents")
    assert all("prompt" not in a for a in response.json()["agents"])
    assert "code review specialist" not in response.text


async def test_agents_does_not_touch_the_runner(client: httpx.AsyncClient) -> None:
    await client.get("/v1/agents")
    assert client.runner.calls == []  # type: ignore[attr-defined]


async def test_agents_requires_a_key_when_one_is_configured(api_settings: Settings) -> None:
    # It enumerates each worker's tools, i.e. the harness's write/exec surface.
    secured = api_settings.model_copy(update={"api_key": "s3cret"})
    async with make_client(secured, FakeRunner()) as c:
        assert (await c.get("/v1/agents")).status_code == 401
        assert (await c.get("/v1/agents", headers={"X-API-Key": "s3cret"})).status_code == 200


async def test_get_agent_returns_the_same_entry_as_the_list(client: httpx.AsyncClient) -> None:
    listed = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
    for name in list(AGENTS) + list(TEAM):
        response = await client.get(f"/v1/agents/{name}")
        assert response.status_code == 200
        # Field for field, not just the name: two independent walks of the rosters are
        # exactly what this endpoint must not become.
        assert response.json() == listed[name]


async def test_get_agent_reports_live_status(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/agents/reviewer")).json()["status"] == "idle"
    REGISTRY.record_start("reviewer")
    body = (await client.get("/v1/agents/reviewer")).json()
    assert body["status"] == "running"
    assert body["invocations"] == 1
    # Stop first: `running` deliberately outranks `failed`, so a failure recorded while
    # the agent is still active would (correctly) still read as running.
    REGISTRY.record_stop("reviewer")
    REGISTRY.record_failure("reviewer")
    assert (await client.get("/v1/agents/reviewer")).json()["status"] == "error"


async def test_unknown_agent_is_a_404_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/agents/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(CONTENT_TYPE)
    body = response.json()
    assert body["type"].endswith("/not-found")
    assert body["status"] == 404
    assert "nope" in body["detail"]
    assert body["instance"] == "/v1/agents/nope"
    # The roster rides along so a stale pinned name is one response away from being fixed.
    assert body["known"] == sorted(AGENTS) + sorted(TEAM)


async def test_agent_lookup_is_exact(client: httpx.AsyncClient) -> None:
    # Case folding here would resolve a name that POST /v1/runs cannot actually invoke,
    # moving the failure from this endpoint to a run that dies mid-flight.
    assert (await client.get("/v1/agents/Critic")).status_code == 404
    assert (await client.get("/v1/agents/critic")).status_code == 200


async def test_absurd_agent_id_is_rejected_before_the_lookup(client: httpx.AsyncClient) -> None:
    # Bounded so a junk path segment cannot be echoed back at length in the error body.
    assert (await client.get(f"/v1/agents/{'x' * 65}")).status_code == 422


async def test_get_agent_never_exposes_a_system_prompt(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/agents/reviewer")
    assert "prompt" not in response.json()
    assert "code review specialist" not in response.text


async def test_get_agent_does_not_touch_the_runner(client: httpx.AsyncClient) -> None:
    await client.get("/v1/agents/critic")
    assert client.runner.calls == []  # type: ignore[attr-defined]


async def test_get_agent_requires_a_key_when_one_is_configured(api_settings: Settings) -> None:
    secured = api_settings.model_copy(update={"api_key": "s3cret"})
    async with make_client(secured, FakeRunner()) as c:
        assert (await c.get("/v1/agents/critic")).status_code == 401
        # Auth before existence: an unauthenticated caller must not be able to probe
        # which agent names are real.
        assert (await c.get("/v1/agents/nope")).status_code == 401
        keyed = {"X-API-Key": "s3cret"}
        assert (await c.get("/v1/agents/critic", headers=keyed)).status_code == 200


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


# --- CORS ----------------------------------------------------------------------------
# The browser-facing contract. Worth testing precisely because it is invisible to curl:
# every assertion here covers something that fails only in a browser, and reports itself
# as the same opaque "CORS error" regardless of which one it was.

DEV_ORIGIN = "http://localhost:3000"
DEV_REGEX = r"http://(localhost|127\.0\.0\.1):\d{1,5}"

PREFLIGHT = {
    "Origin": DEV_ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type,x-api-key",
}


def cors_settings(base: Settings, **overrides: Any) -> Settings:
    """Pin every CORS field, always.

    Without this the assertions below silently depend on whatever the developer happens
    to have in their `.env` -- and the negative cases ("no CORS configured") would start
    failing the moment someone sets the origins for real.
    """
    return base.model_copy(
        update={
            "api_cors_origins": "",
            "api_cors_origin_regex": None,
            "api_cors_allow_credentials": False,
            **overrides,
        }
    )


async def test_no_cors_configured_means_no_cors_headers(api_settings: Settings) -> None:
    async with make_client(cors_settings(api_settings), FakeRunner()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers={"Origin": DEV_ORIGIN})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_is_allowed_for_a_configured_origin(api_settings: Settings) -> None:
    configured = cors_settings(api_settings, api_cors_origins=DEV_ORIGIN)
    async with make_client(configured, FakeRunner()) as c:
        response = await c.options("/v1/runs", headers=PREFLIGHT)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "x-api-key" in response.headers["access-control-allow-headers"].lower()


async def test_preflight_never_reaches_auth(api_settings: Settings) -> None:
    # CORSMiddleware answers the preflight above the router, so it must succeed even with
    # a key configured -- browsers do not send credentials on a preflight, and a 401 here
    # would make an authenticated API unusable from any frontend.
    secured = cors_settings(api_settings, api_cors_origins=DEV_ORIGIN, api_key="s3cret")
    async with make_client(secured, FakeRunner()) as c:
        assert (await c.options("/v1/runs", headers=PREFLIGHT)).status_code == 200


async def test_a_simple_request_echoes_the_origin_and_exposes_headers(
    api_settings: Settings,
) -> None:
    configured = cors_settings(api_settings, api_cors_origins=f"{DEV_ORIGIN},https://app.example")
    async with make_client(configured, FakeRunner()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers={"Origin": DEV_ORIGIN})
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "x-request-id" in exposed
    assert "retry-after" in exposed  # a client cannot back off from a 503 it cannot read


async def test_an_unconfigured_origin_is_refused(api_settings: Settings) -> None:
    configured = cors_settings(api_settings, api_cors_origins=DEV_ORIGIN)
    async with make_client(configured, FakeRunner()) as c:
        response = await c.options(
            "/v1/runs", headers={**PREFLIGHT, "Origin": "https://evil.example"}
        )
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://localhost:3000", True),
        ("http://localhost:5173", True),
        ("http://127.0.0.1:8080", True),
        ("https://localhost:3000", False),  # scheme is part of the origin
        ("http://localhost.evil.example", False),
        ("http://localhost:3000.evil.example", False),  # fullmatch, not search
    ],
)
async def test_the_origin_regex_matches_dev_ports_and_nothing_else(
    api_settings: Settings, origin: str, allowed: bool
) -> None:
    configured = cors_settings(api_settings, api_cors_origin_regex=DEV_REGEX)
    async with make_client(configured, FakeRunner()) as c:
        response = await c.options("/v1/runs", headers={**PREFLIGHT, "Origin": origin})
    assert ("access-control-allow-origin" in response.headers) is allowed


async def test_wildcard_origin_drops_credentials_rather_than_breaking(
    api_settings: Settings,
) -> None:
    # `Allow-Origin: *` with `Allow-Credentials: true` is rejected by every browser. Given
    # the pair, keep the wildcard working rather than shipping a config that looks right
    # and fails on every request.
    configured = cors_settings(api_settings, api_cors_origins="*", api_cors_allow_credentials=True)
    async with make_client(configured, FakeRunner()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers={"Origin": DEV_ORIGIN})
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


async def test_credentials_are_sent_for_an_exact_origin_when_enabled(
    api_settings: Settings,
) -> None:
    configured = cors_settings(
        api_settings, api_cors_origins=DEV_ORIGIN, api_cors_allow_credentials=True
    )
    async with make_client(configured, FakeRunner()) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers={"Origin": DEV_ORIGIN})
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_allowed_methods_are_derived_from_the_router(api_settings: Settings) -> None:
    # Guards against the hardcoded list drifting from the routes as endpoints are added.
    configured = cors_settings(api_settings, api_cors_origins=DEV_ORIGIN)
    async with make_client(configured, FakeRunner()) as c:
        response = await c.options("/v1/runs", headers=PREFLIGHT)
    allowed = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert {"GET", "POST", "OPTIONS"} <= allowed


# --- CORS headers on failures --------------------------------------------------------
# The failure mode these cover: an error response without CORS headers is unreadable to
# the browser, so the frontend reports a CORS problem and the actual status and problem
# document -- the things that say what went wrong -- never surface.


@pytest.mark.parametrize(
    ("path", "payload", "expected"),
    [
        ("/v1/runs", {}, 422),  # validation
        ("/v1/nope", None, 404),  # routing
    ],
)
async def test_handled_errors_carry_cors_headers(
    api_settings: Settings, path: str, payload: dict[str, Any] | None, expected: int
) -> None:
    configured = cors_settings(api_settings, api_cors_origins=DEV_ORIGIN)
    async with make_client(configured, FakeRunner()) as c:
        response = await c.post(path, json=payload, headers={"Origin": DEV_ORIGIN})
    assert response.status_code == expected
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN


async def test_an_unhandled_exception_is_a_500_that_carries_cors_headers(
    api_settings: Settings,
) -> None:
    # FastAPI hands the `Exception` handler to ServerErrorMiddleware, which sits *outside*
    # CORS -- so without `ProblemFallbackMiddleware` this response has no CORS headers and
    # the browser hides a 500 behind a CORS error. Raising from a dependency puts the
    # exception outside the route's own try/except, which is the path that reaches it.
    def boom() -> None:
        raise RuntimeError("something nobody planned for")

    configured = cors_settings(api_settings, api_cors_origins=DEV_ORIGIN)
    app = create_app(configured, runner=FakeRunner())
    app.dependency_overrides[get_slots] = boom
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post("/v1/runs", json={"prompt": "x"}, headers={"Origin": DEV_ORIGIN})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    assert response.headers["content-type"].startswith(CONTENT_TYPE)
    assert response.json()["type"].endswith("/internal-error")
    assert "nobody planned for" not in response.text  # internals stay in the log


async def test_an_error_response_carries_exactly_one_request_id(
    client: httpx.AsyncClient,
) -> None:
    # `problem()` sets the header and the middleware used to append it again, so clients
    # read "abc, abc" on every error while the 200 path looked fine.
    response = await client.post("/v1/runs", json={}, headers={"x-request-id": "trace-abc"})
    assert response.status_code == 422
    assert response.headers["x-request-id"] == "trace-abc"
