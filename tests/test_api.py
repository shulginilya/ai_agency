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
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agency.agents.definitions import AGENTS
from agency.agents.team import TEAM
from agency.api.app import create_app
from agency.api.deps import _AgentConversation, get_runner, get_slots
from agency.api.errors import CONTENT_TYPE
from agency.api.sessions import Conversation, SessionStore
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


OAUTH_EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"
"""Verbatim what the SDK returns when the CLI subprocess has no usable credentials.

Kept as the literal string rather than something shorter: this is matched by substring
in `RunRecord.auth_failure`, so a test that invented its own wording would assert the
mapping without ever exercising the marker list that has to recognise the real one.
"""


def _auth_failed_record() -> RunRecord:
    """A run that never reached the model. `is_error` and prose -- the SDK gives the
    same shape for this as for "I could not do that", which is the whole difficulty."""
    return _finished_record(is_error=True, output=OAUTH_EXPIRED, num_turns=1, total_cost_usd=0.0)


class FakeRunner:
    """Stands in for `runner.run_once`. Records what the route asked for."""

    def __init__(self, record: RunRecord | None = None, delay: float = 0.0) -> None:
        self.record = record or _finished_record()
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        # Which task the run actually executed in. See the timeout-mechanism tests.
        self.tasks: list[Any] = []

    async def __call__(
        self, prompt: str, *, profile: Profile | None = None, team: bool = False
    ) -> RunRecord:
        self.calls.append({"prompt": prompt, "profile": profile, "team": team})
        self.tasks.append(asyncio.current_task())
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.record


class FakeConversation:
    """Stands in for a connected `runner.Agent`.

    Keeps its own turn counter and prompt log, which is what makes "the context was
    retained" assertable without a model: if two messages reach the *same* object, the
    session held its conversation open, and if they do not, it did not.
    """

    def __init__(
        self,
        profile: Profile,
        team: bool,
        delay: float = 0.0,
        boom: str = "",
        auth_fail: bool = False,
        close_delay: float = 0.0,
    ) -> None:
        self.profile = profile
        self.team = team
        self.delay = delay
        self.boom = boom
        # Teardown is slow and can fail independently of the turn: the SDK bounds its
        # own close at ~20s, which is what makes a sequential `aclose_all` a problem.
        self.close_delay = close_delay
        self.close_boom = ""
        # Distinct from `boom`: a credential failure is a turn that *completed* and
        # returned an errored record, not a transport exception. The two take different
        # paths through the route and only one of them may close the session.
        self.auth_fail = auth_fail
        self.prompts: list[str] = []
        self.closed = 0
        # Which task the turn actually executed in. See the timeout-mechanism tests.
        self.tasks: list[Any] = []

    async def send(self, prompt: str) -> RunRecord:
        self.prompts.append(prompt)
        self.tasks.append(asyncio.current_task())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError(self.boom)
        if self.auth_fail:
            return _auth_failed_record()
        return _finished_record(run_id=f"turn{len(self.prompts)}", prompt=prompt)

    async def aclose(self) -> None:
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        # Counted, not flagged: a store that closed a session twice would leak nothing
        # but would mean `close` is running on a path nobody reasoned about. Counted
        # *before* the raise, so "we tried" stays assertable for a wedged teardown.
        self.closed += 1
        if self.close_boom:
            raise RuntimeError(self.close_boom)


class FakeConversations:
    """Stands in for `deps.make_conversation_factory`. Records what was opened."""

    def __init__(
        self,
        delay: float = 0.0,
        boom: str = "",
        auth_fail: bool = False,
        close_delay: float = 0.0,
    ) -> None:
        self.delay = delay
        self.boom = boom
        self.auth_fail = auth_fail
        self.close_delay = close_delay
        self.opened: list[FakeConversation] = []

    async def __call__(self, *, profile: Profile, team: bool = False) -> Conversation:
        conversation = FakeConversation(
            profile,
            team,
            delay=self.delay,
            boom=self.boom,
            auth_fail=self.auth_fail,
            close_delay=self.close_delay,
        )
        self.opened.append(conversation)
        return conversation


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"api_run_timeout_s": 5.0, "api_docs_enabled": True})


def make_client(
    settings: Settings, runner: Any, conversations: Any | None = None
) -> httpx.AsyncClient:
    app = create_app(
        settings,
        runner=runner,
        conversation_factory=conversations or FakeConversations(),
    )
    # dependency_overrides as well as the constructor argument: the constructor covers
    # production wiring, the override proves the seam works for anyone extending this.
    app.dependency_overrides[get_runner] = lambda: runner
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def client(api_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    runner = FakeRunner()
    conversations = FakeConversations()
    async with make_client(api_settings, runner, conversations) as c:
        c.runner = runner  # type: ignore[attr-defined]
        c.conversations = conversations  # type: ignore[attr-defined]
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


async def test_a_credential_failure_is_a_502_not_a_200(api_settings: Settings) -> None:
    # The exception to the rule above, and the reason the rule needs one. An expired or
    # missing credential arrives wearing a model failure's clothes -- is_error plus
    # prose -- but nothing about the request was wrong and no retry will help. Left as a
    # 200 it reaches the browser as a chat bubble reading "OAuth session expired", which
    # is the agent apparently talking about the server's own configuration.
    runner = FakeRunner(_auth_failed_record())
    async with make_client(api_settings, runner) as c:
        response = await c.post("/v1/runs", json={"prompt": "x"})
    assert response.status_code == 502
    body = response.json()
    assert body["type"].endswith("/agent-auth-failed")
    # Actionable and machine-readable: the type URI is what a client switches on, the
    # detail is what the operator reads.
    assert "ANTHROPIC_API_KEY" in body["detail"]
    assert response.headers["content-type"].startswith(CONTENT_TYPE)


async def test_an_agent_talking_about_api_keys_is_not_an_auth_failure(
    api_settings: Settings,
) -> None:
    # The marker match is on the result text, so the guard that keeps it honest is
    # `is_error`. An agent that succeeds while *writing* about an invalid API key --
    # reviewing auth code, say -- is a 200 like any other.
    runner = FakeRunner(_finished_record(output="Your code passes an invalid API key here."))
    async with make_client(api_settings, runner) as c:
        response = await c.post("/v1/runs", json={"prompt": "review this"})
    assert response.status_code == 200


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


# --- sessions ------------------------------------------------------------------------
# A session is the only resource here that outlives a request: it holds an SDK
# subprocess open between calls. So these tests are less about the mapping and more
# about the lifecycle -- who closes it, what happens when nobody does, and which
# failures leave it usable.


async def _open(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/v1/sessions", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_opening_a_session_returns_a_handle(client: httpx.AsyncClient) -> None:
    body = await _open(client)
    assert body["id"]
    assert body["status"] == "idle"
    assert body["turns"] == 0
    # The server's own profile, resolved here rather than reported as null -- a client
    # must be able to see what it is actually talking to.
    assert body["profile"] == "sandboxed"
    assert body["expires_at"] is not None


async def test_session_options_are_pinned_at_open_time(client: httpx.AsyncClient) -> None:
    body = await _open(client, profile="eval", team=True)
    conversation = client.conversations.opened[-1]  # type: ignore[attr-defined]
    assert (body["profile"], body["team"]) == ("eval", True)
    assert (conversation.profile, conversation.team) == ("eval", True)


async def test_a_message_returns_the_mapped_turn(client: httpx.AsyncClient) -> None:
    session = await _open(client)
    response = await client.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session["id"]
    assert body["turn"] == 1
    # `run` is the same object POST /v1/runs returns, so a client renders both the same
    # way. Nesting it is what keeps that true as either side grows fields.
    assert body["run"]["run_id"] == "turn1"
    assert body["run"]["tools_used"] == ["Read", "Agent"]
    assert body["run"]["delegated_to"] == ["reviewer"]


async def test_turns_share_one_retained_conversation(client: httpx.AsyncClient) -> None:
    # The entire point of the endpoint. Two messages must reach the same live agent --
    # a per-request agent would answer turn two with no memory of turn one, which is
    # exactly what POST /v1/runs already does.
    session = await _open(client)
    for prompt in ("first", "second"):
        await client.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": prompt})

    assert len(client.conversations.opened) == 1  # type: ignore[attr-defined]
    assert client.conversations.opened[0].prompts == ["first", "second"]  # type: ignore[attr-defined]
    body = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert body["turns"] == 2


async def test_a_message_may_not_smuggle_run_options(client: httpx.AsyncClient) -> None:
    # 422, not a silent no-op: profile and team were baked into the options this session
    # connected with, so accepting them here would tell the caller they had switched
    # profile mid-conversation when they had not.
    session = await _open(client)
    for body in ({"prompt": "x", "profile": "eval"}, {"prompt": "x", "team": True}):
        response = await client.post(f"/v1/sessions/{session['id']}/messages", json=body)
        assert response.status_code == 422


async def test_a_second_message_while_one_is_in_flight_is_409(api_settings: Settings) -> None:
    # Refused, not queued: two overlapping turns on one SDK client interleave into a
    # single conversation, and the second caller drains the first one\'s response.
    conversations = FakeConversations(delay=0.2)
    async with make_client(api_settings, FakeRunner(), conversations) as c:
        session = await _open(c)
        url = f"/v1/sessions/{session['id']}/messages"
        first = asyncio.create_task(c.post(url, json={"prompt": "slow"}))
        await asyncio.sleep(0.05)
        assert (await c.get(f"/v1/sessions/{session['id']}")).json()["status"] == "busy"
        second = await c.post(url, json={"prompt": "impatient"})
        assert (await first).status_code == 200

    assert second.status_code == 409
    assert second.json()["type"].endswith("/session-busy")
    # And the refusal never reached the agent.
    assert conversations.opened[0].prompts == ["slow"]


async def test_closing_a_session_releases_the_conversation(client: httpx.AsyncClient) -> None:
    session = await _open(client)
    assert (await client.delete(f"/v1/sessions/{session['id']}")).status_code == 204
    assert client.conversations.opened[0].closed == 1  # type: ignore[attr-defined]
    # The handle is dead, and says so rather than 204-ing again.
    assert (await client.get(f"/v1/sessions/{session['id']}")).status_code == 404
    assert (await client.delete(f"/v1/sessions/{session['id']}")).status_code == 404


async def test_closing_a_busy_session_is_refused(api_settings: Settings) -> None:
    # Tearing the client out mid-turn would surface to the other caller as an
    # unexplainable 502. The turn has its own timeout, so the wait is bounded.
    conversations = FakeConversations(delay=0.2)
    async with make_client(api_settings, FakeRunner(), conversations) as c:
        session = await _open(c)
        turn = asyncio.create_task(
            c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "slow"})
        )
        await asyncio.sleep(0.05)
        refused = await c.delete(f"/v1/sessions/{session['id']}")
        assert (await turn).status_code == 200
        assert refused.status_code == 409
        assert conversations.opened[0].closed == 0
        # Once it is idle again the close goes through.
        assert (await c.delete(f"/v1/sessions/{session['id']}")).status_code == 204


async def test_unknown_session_is_a_404_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/sessions/nope/messages", json={"prompt": "x"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(CONTENT_TYPE)
    body = response.json()
    assert body["type"].endswith("/not-found")
    # The detail names expiry, because that is the likeliest reason a handle the client
    # definitely received has stopped working.
    assert "expired" in body["detail"]


async def test_absurd_session_id_is_rejected_before_the_lookup(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/v1/sessions/{'x' * 65}")).status_code == 422


async def test_the_session_cap_refuses_with_503(api_settings: Settings) -> None:
    bounded = api_settings.model_copy(update={"api_max_sessions": 1})
    conversations = FakeConversations()
    async with make_client(bounded, FakeRunner(), conversations) as c:
        await _open(c)
        refused = await c.post("/v1/sessions", json={})
        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "5"
        assert refused.json()["type"].endswith("/overloaded")
        # Nothing was connected for the refused request -- the cap is checked first, or
        # it would be a cap on *reported* sessions rather than on subprocesses.
        assert len(conversations.opened) == 1


async def test_an_idle_session_is_reaped(api_settings: Settings) -> None:
    brief = api_settings.model_copy(update={"api_session_idle_ttl_s": 0.05})
    conversations = FakeConversations()
    async with make_client(brief, FakeRunner(), conversations) as c:
        session = await _open(c)
        await asyncio.sleep(0.1)
        # Reaping is lazy: this request is what triggers it. A client that crashed
        # mid-conversation never sends its DELETE, and the subprocess would otherwise
        # live until the container restarts.
        assert (await c.get(f"/v1/sessions/{session['id']}")).status_code == 404
        assert conversations.opened[0].closed == 1
        assert (await c.get("/v1/sessions")).json()["count"] == 0


async def test_the_ttl_can_be_disabled(api_settings: Settings) -> None:
    forever = api_settings.model_copy(update={"api_session_idle_ttl_s": 0.0})
    async with make_client(forever, FakeRunner()) as c:
        session = await _open(c)
        assert session["expires_at"] is None
        await asyncio.sleep(0.05)
        assert (await c.get(f"/v1/sessions/{session['id']}")).status_code == 200


async def test_a_timed_out_turn_closes_the_session(api_settings: Settings) -> None:
    # The turn is cancelled mid-drain, which leaves the SDK client connected but its
    # response stream half-consumed. Reusing that would produce a wrong answer rather
    # than an error, so the session goes with it -- and the body says so.
    impatient = api_settings.model_copy(update={"api_run_timeout_s": 0.05})
    conversations = FakeConversations(delay=5.0)
    async with make_client(impatient, FakeRunner(), conversations) as c:
        session = await _open(c)
        response = await c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
        assert response.status_code == 504
        assert response.json()["type"].endswith("/run-timeout")
        assert "closed" in response.json()["detail"]
        assert conversations.opened[0].closed == 1
        assert (await c.get(f"/v1/sessions/{session['id']}")).status_code == 404


async def test_a_failed_turn_closes_the_session_without_leaking_internals(
    api_settings: Settings,
) -> None:
    conversations = FakeConversations(boom="ANTHROPIC_API_KEY=sk-secret is invalid")
    async with make_client(api_settings, FakeRunner(), conversations) as c:
        session = await _open(c)
        response = await c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
        assert response.status_code == 502
        assert "sk-secret" not in response.text
        assert "RuntimeError" in response.json()["detail"]
        assert conversations.opened[0].closed == 1
        assert (await c.get(f"/v1/sessions/{session['id']}")).status_code == 404


async def test_a_credential_failure_does_not_close_the_session(api_settings: Settings) -> None:
    # The counterpart to the timeout and transport cases above: those leave the SDK
    # client in an unknown state, so the session dies with them. This one does not --
    # the turn completed and simply never reached the model, so the conversation is
    # intact and will work again the moment the server has credentials. Killing it would
    # cost the user their context over a problem that is entirely ours.
    conversations = FakeConversations(auth_fail=True)
    async with make_client(api_settings, FakeRunner(), conversations) as c:
        session = await _open(c)
        response = await c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
        assert response.status_code == 502
        assert response.json()["type"].endswith("/agent-auth-failed")
        assert conversations.opened[0].closed == 0

        alive = await c.get(f"/v1/sessions/{session['id']}")
        assert alive.status_code == 200
        # And the turn is not counted: it never reached the model, so counting it would
        # overstate the conversation to anyone reading `turns`.
        assert alive.json()["turns"] == 0


async def test_a_turn_takes_a_run_slot(api_settings: Settings) -> None:
    # Sessions must not be a way around the run cap: an open session is idle weight, but
    # a running turn costs exactly what a POST /v1/runs costs.
    bounded = api_settings.model_copy(update={"api_max_concurrent_runs": 1})
    conversations = FakeConversations(delay=0.2)
    async with make_client(bounded, FakeRunner(), conversations) as c:
        session = await _open(c)
        turn = asyncio.create_task(
            c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "slow"})
        )
        await asyncio.sleep(0.05)
        run = await c.post("/v1/runs", json={"prompt": "queued"})
        assert (await turn).status_code == 200
    assert run.status_code == 503


async def test_a_saturated_run_pool_does_not_kill_the_session(api_settings: Settings) -> None:
    # The load-bearing half of the "close a broken session" rule: a 503 means the turn
    # never ran, so the conversation is untouched. Closing it here would turn a
    # retryable refusal into permanent loss of the context.
    bounded = api_settings.model_copy(update={"api_max_concurrent_runs": 1})
    runner = FakeRunner(delay=0.2)
    conversations = FakeConversations()
    async with make_client(bounded, runner, conversations) as c:
        session = await _open(c)
        occupied = asyncio.create_task(c.post("/v1/runs", json={"prompt": "slow"}))
        await asyncio.sleep(0.05)
        refused = await c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
        assert refused.status_code == 503
        assert (await occupied).status_code == 200

        assert conversations.opened[0].closed == 0
        retried = await c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
        assert retried.status_code == 200
        assert retried.json()["turn"] == 1


async def test_listing_sessions_tracks_the_lifecycle(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/sessions")).json() == {"sessions": [], "count": 0}
    first = await _open(client)
    second = await _open(client)
    body = (await client.get("/v1/sessions")).json()
    assert [s["id"] for s in body["sessions"]] == [first["id"], second["id"]]
    assert body["count"] == 2

    await client.delete(f"/v1/sessions/{first['id']}")
    body = (await client.get("/v1/sessions")).json()
    assert [s["id"] for s in body["sessions"]] == [second["id"]]


async def test_readyz_reports_session_capacity(client: httpx.AsyncClient) -> None:
    await _open(client)
    capacity = (await client.get("/readyz")).json()["capacity"]
    assert capacity["sessions_open"] == 1
    assert capacity["session_limit"] >= 1


async def test_sessions_never_touch_the_one_shot_runner(client: httpx.AsyncClient) -> None:
    # Two seams, not one. A session that fell back to `run_once` would silently lose the
    # retained context -- and would still pass every mapping assertion above.
    session = await _open(client)
    await client.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
    assert client.runner.calls == []  # type: ignore[attr-defined]


async def test_session_routes_require_a_key_when_one_is_configured(
    api_settings: Settings,
) -> None:
    secured = api_settings.model_copy(update={"api_key": "s3cret"})
    keyed = {"X-API-Key": "s3cret"}
    async with make_client(secured, FakeRunner()) as c:
        assert (await c.post("/v1/sessions", json={})).status_code == 401
        assert (await c.get("/v1/sessions")).status_code == 401
        # Auth before existence: an unauthenticated caller must not be able to probe
        # which session ids are live.
        assert (await c.get("/v1/sessions/whatever")).status_code == 401
        assert (await c.delete("/v1/sessions/whatever")).status_code == 401
        assert (
            await c.post("/v1/sessions/whatever/messages", json={"prompt": "x"})
        ).status_code == 401

        opened = await c.post("/v1/sessions", json={}, headers=keyed)
        assert opened.status_code == 201
        session_id = opened.json()["id"]
        sent = await c.post(
            f"/v1/sessions/{session_id}/messages", json={"prompt": "x"}, headers=keyed
        )
        assert sent.status_code == 200


async def test_openapi_documents_the_session_contract(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "409" in paths["/v1/sessions/{session_id}/messages"]["post"]["responses"]
    assert "503" in paths["/v1/sessions"]["post"]["responses"]
    assert "delete" in paths["/v1/sessions/{session_id}"]


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


# --- session teardown ----------------------------------------------------------------
# Every lifecycle test above ends with someone asking the server a question. These are
# about the case where nobody does. Both were live failures: a frontend whose "close"
# sent a prompt instead of a DELETE left a CLI subprocess running indefinitely, and
# Ctrl-C then appeared to hang while the survivors were torn down one at a time.
#
# None of this is reachable through `client`: the reaper task and `aclose_all` belong to
# the lifespan, and `httpx.ASGITransport` never runs lifespan events. So these drive the
# store directly -- the same reason `SessionStore.reap` stayed independently callable.


def _store(conversations: Any, idle_ttl_s: float = 0.05, limit: int = 8) -> SessionStore:
    return SessionStore(conversations, limit=limit, idle_ttl_s=idle_ttl_s)


async def _drain(store: SessionStore, interval: float = 0.01, tries: int = 200) -> None:
    """Run the reaper until the store empties, then stop it."""
    reaper = asyncio.create_task(store.reap_forever(interval))
    try:
        for _ in range(tries):
            await asyncio.sleep(interval)
            if store.count == 0:
                return
    finally:
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper


async def test_the_reaper_collects_a_session_nobody_asks_about() -> None:
    # The whole point of the timer. Nothing touches the store after `open`, which is
    # exactly the state a client that closed its tab leaves behind -- and the state in
    # which the lazy reap in open/get/list never runs, so the TTL is never once
    # evaluated and the subprocess outlives everything.
    conversations = FakeConversations()
    store = _store(conversations)
    session = await store.open(profile="sandboxed")
    await _drain(store)
    assert store.count == 0
    assert session.closed
    assert conversations.opened[0].closed == 1


async def test_the_reaper_does_not_die_on_a_failed_reap() -> None:
    # A reaper that dies is indistinguishable from no reaper at all, and the symptom
    # arrives hours later as a machine full of CLI subprocesses. One bad pass must not
    # be able to end the loop.
    conversations = FakeConversations()
    store = _store(conversations)
    await store.open(profile="sandboxed")
    passes = 0
    real_reap = store.reap

    async def flaky() -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            raise RuntimeError("the first pass blew up")
        await real_reap()

    store.reap = flaky  # type: ignore[method-assign]
    await _drain(store)
    assert passes > 1
    assert store.count == 0
    assert conversations.opened[0].closed == 1


async def test_the_reaper_does_not_run_when_the_ttl_is_disabled() -> None:
    store = _store(FakeConversations(), idle_ttl_s=0.0)
    assert store.reap_interval_s == 0.0
    # Returns rather than looping forever: a disabled TTL means "hold sessions until
    # someone asks", and a task that woke only to do nothing would be a lie in a trace.
    await asyncio.wait_for(store.reap_forever(), timeout=1.0)


def test_the_reap_interval_is_derived_and_clamped() -> None:
    # A quarter of the TTL, floored so a tiny TTL cannot make this a busy loop and
    # capped so a long one is still checked within a minute of expiry.
    assert _store(FakeConversations(), idle_ttl_s=900.0).reap_interval_s == 60.0
    assert _store(FakeConversations(), idle_ttl_s=40.0).reap_interval_s == 10.0
    assert _store(FakeConversations(), idle_ttl_s=0.05).reap_interval_s == 1.0


async def test_shutdown_closes_sessions_at_once_not_one_at_a_time() -> None:
    # The assertion that matters is the wall clock -- "they all closed" was already true
    # of the sequential version. The SDK bounds each teardown at roughly twenty seconds,
    # so sequentially a full box is minutes of a terminal that looks hung after Ctrl-C,
    # with the second Ctrl-C unable to get through the SDK's shielded close.
    conversations = FakeConversations(close_delay=0.2)
    store = _store(conversations, idle_ttl_s=0.0)
    for _ in range(5):
        await store.open(profile="sandboxed")
    started = time.perf_counter()
    await store.aclose_all()
    elapsed = time.perf_counter() - started
    assert store.count == 0
    assert all(c.closed == 1 for c in conversations.opened)
    assert elapsed < 0.5, f"closed sequentially: {elapsed:.2f}s for 5 x 0.2s"


async def test_one_wedged_teardown_does_not_strand_the_rest() -> None:
    # `close` already logs and swallows, so an exception reaching `aclose_all` is a bug
    # in the bookkeeping -- and aborting the gather would leave the remaining sessions
    # open, which is the exact leak the method exists to prevent.
    conversations = FakeConversations()
    store = _store(conversations, idle_ttl_s=0.0)
    for _ in range(3):
        await store.open(profile="sandboxed")
    conversations.opened[0].close_boom = "wedged"
    await store.aclose_all()
    assert store.count == 0
    assert all(c.closed == 1 for c in conversations.opened)


# --- the orphan backstop -------------------------------------------------------------
# `_AgentConversation` is the only object in `api/` that holds an SDK handle, so it is
# the only place that can tell whether the CLI child actually died. It has to, because
# `SubprocessCLITransport.close()` shields its own teardown and an anyio shield does not
# defer a *raw* asyncio cancellation -- uvicorn cancelling a request task when the
# browser hangs up, or the loop shutting down under Ctrl-C. The escalation that kills the
# child is then skipped. The backstop is synchronous for exactly that reason.


class FakeProcess:
    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode


class FakeAgent:
    """The attribute chain `_child_process` reaches through, plus an exit that can fail."""

    def __init__(self, process: Any, boom: BaseException | None = None) -> None:
        self._client = SimpleNamespace(_transport=SimpleNamespace(_process=process))
        self._boom = boom
        self.exited = 0

    async def __aexit__(self, *_exc: object) -> None:
        self.exited += 1
        if self._boom is not None:
            raise self._boom


class ShapelessAgent:
    """No `_client`. Stands in for the day the SDK's internals move."""

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def killed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    pids: list[int] = []
    monkeypatch.setattr("agency.api.deps._kill_process_tree", pids.append)
    return pids


async def test_a_clean_teardown_kills_nothing(killed: list[int]) -> None:
    agent = FakeAgent(FakeProcess(returncode=0))
    await _AgentConversation(agent).aclose()
    assert agent.exited == 1
    assert killed == []


async def test_a_child_that_outlived_its_own_close_is_killed(killed: list[int]) -> None:
    # `returncode is None` means nothing reaped it, so the pid is still ours and cannot
    # have been reissued to an unrelated process.
    await _AgentConversation(FakeAgent(FakeProcess())).aclose()
    assert killed == [4242]


async def test_the_backstop_runs_even_when_the_teardown_is_cancelled(
    killed: list[int],
) -> None:
    # The reason it is a synchronous `finally` and not an await: this runs while
    # CancelledError is propagating, which is precisely what the SDK's shield cannot
    # promise when the cancellation came from asyncio rather than from anyio.
    conversation = _AgentConversation(FakeAgent(FakeProcess(), boom=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await conversation.aclose()
    assert killed == [4242]


async def test_the_backstop_runs_when_the_teardown_raises(killed: list[int]) -> None:
    conversation = _AgentConversation(FakeAgent(FakeProcess(), boom=RuntimeError("wedged")))
    with pytest.raises(RuntimeError):
        await conversation.aclose()
    assert killed == [4242]


async def test_losing_the_backstop_does_not_break_the_teardown(killed: list[int]) -> None:
    # Three private attributes deep into a third-party package. When that stops
    # resolving, silently having no backstop is the correct failure; raising in the
    # middle of a close is not, because the close is what everything else depends on.
    await _AgentConversation(ShapelessAgent()).aclose()
    assert killed == []


async def test_the_lifespan_owns_the_reaper_and_closes_what_is_left(
    api_settings: Settings,
) -> None:
    # The wiring itself, driven directly. `httpx.ASGITransport` never runs lifespan
    # events, so no other test in this file can tell whether the reaper is actually
    # started, whether it is cancelled again, or whether shutdown closes the sessions the
    # process was still holding -- and "the container stopped without orphaning one
    # subprocess per open session" is the only thing this lifespan is for.
    conversations = FakeConversations()
    app = create_app(api_settings, runner=FakeRunner(), conversation_factory=conversations)
    store = app.state.sessions
    async with app.router.lifespan_context(app):
        assert "agency.session-reaper" in {t.get_name() for t in asyncio.all_tasks()}
        await store.open(profile="sandboxed")
        assert store.count == 1
    assert store.count == 0
    assert conversations.opened[0].closed == 1
    # Cancelled, not merely abandoned: a loop that outlived its app would hold a store
    # nothing can reach and keep waking up to reap it.
    assert "agency.session-reaper" not in {t.get_name() for t in asyncio.all_tasks()}


# --- how the timeout cancels ---------------------------------------------------------
# Not "does it time out" -- that is asserted above -- but *how*, which is the difference
# between a reaped CLI subprocess and an orphaned one.
#
# `asyncio.wait_for` runs the awaitable in a child task and cancels that task, which is a
# raw asyncio cancellation. `SubprocessCLITransport.close()` documents that its shield
# only defers cancellation originating from an anyio cancel scope, so a raw one is
# delivered at the first await of the teardown and the terminate -> kill escalation below
# it never runs: the child survives the timeout that existed to reap it. An anyio scope
# cancels the task in place instead, the shield holds, and the child dies.
#
# A fake cannot show the surviving subprocess, so these assert the mechanism that decides
# it: same task means the cancellation came from a scope, a child task means `wait_for`.
# Sound only because this app's middleware is pure ASGI -- `BaseHTTPMiddleware` would run
# the route in a task of its own and this would be comparing two unrelated tasks. See
# `api/middleware.py` for why it is not used.


async def test_a_turn_is_cancelled_in_place_not_through_a_wrapper_task(
    api_settings: Settings,
) -> None:
    conversations = FakeConversations()
    async with make_client(api_settings, FakeRunner(), conversations) as c:
        session = await _open(c)
        here = asyncio.current_task()
        response = await c.post(f"/v1/sessions/{session['id']}/messages", json={"prompt": "x"})
        assert response.status_code == 200
        assert conversations.opened[0].tasks == [here]


async def test_a_run_is_cancelled_in_place_not_through_a_wrapper_task(
    api_settings: Settings,
) -> None:
    runner = FakeRunner()
    async with make_client(api_settings, runner) as c:
        here = asyncio.current_task()
        assert (await c.post("/v1/runs", json={"prompt": "x"})).status_code == 200
        assert runner.tasks == [here]
