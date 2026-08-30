"""The HTTP surface for capability grants: requests, responses, and the admin routes.

No database and no model. The capability store is injected, exactly as `Runner` is --
an API change that needs a live Postgres to test is the same design smell as one that
needs a live model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from agency.api.app import create_app
from agency.capabilities.db_store import DbCapabilityStore, seed_registry
from agency.capabilities.model import ResolvedGrants
from agency.capabilities.seed import CAPABILITIES
from agency.capabilities.store import SeedCapabilityStore
from agency.config import Profile, Settings
from agency.db.base import Base
from agency.db.engine import dispose_engines, make_engine, make_session_factory
from agency.obs.cost import RunRecord

ADMIN_KEY = "admin-secret"


def _record() -> RunRecord:
    record = RunRecord(run_id="r1", prompt="x", profile="dev", model="m")
    record.finished_at = "2026-08-29T00:00:00+00:00"
    record.output = "done"
    return record


class CapturingRunner:
    """Records the `ResolvedGrants` the route handed over -- the thing under test."""

    def __init__(self) -> None:
        self.grants: ResolvedGrants | None = None

    async def __call__(
        self,
        prompt: str,
        *,
        profile: Profile | None = None,
        team: str | None = None,
        grants: ResolvedGrants | None = None,
    ) -> RunRecord:
        self.grants = grants
        return _record()


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        profile="dev",
        workspace_dir=tmp_path / "workspace",
        run_dir=tmp_path / "runs",
        log_format="json",
        anthropic_api_key=None,
        anthropic_auth_token=None,
        claude_code_oauth_token=None,
    )


def _client(settings: Settings, **kwargs: object) -> httpx.AsyncClient:
    app = create_app(settings, **kwargs)  # type: ignore[arg-type]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- reading works with no database --------------------------------------------------


async def test_capabilities_are_listed_from_the_seed_when_there_is_no_database(
    api_settings: Settings,
) -> None:
    async with _client(api_settings, runner=CapturingRunner()) as client:
        response = await client.get("/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "seed"
    assert {c["name"] for c in body["capabilities"]} == set(CAPABILITIES)


async def test_the_agent_roster_publishes_what_each_agent_may_and_may_not_hold(
    api_settings: Settings,
) -> None:
    """`barred` is published on purpose: a ceiling nobody can see before they hit it
    reads as an arbitrary refusal."""
    async with _client(api_settings, runner=CapturingRunner()) as client:
        response = await client.get("/v1/agents")
    by_name = {a["name"]: a for a in response.json()["agents"]}
    assert "shell" in by_name["reviewer"]["barred"]
    assert "shell" in by_name["developer"]["grantable"]
    assert "web-research" in by_name["reviewer"]["grantable"]


# --- grants on a run ------------------------------------------------------------------


async def test_a_run_request_resolves_grants_before_reaching_the_runner(
    api_settings: Settings,
) -> None:
    runner = CapturingRunner()
    async with _client(api_settings, runner=runner) as client:
        response = await client.post(
            "/v1/runs",
            json={"prompt": "x", "grants": {"lead": ["web-research"]}},
        )
    assert response.status_code == 200
    assert runner.grants is not None
    assert runner.grants.granted_tools() == frozenset({"WebSearch", "WebFetch"})
    # And the response echoes what actually resolved, not what was asked for.
    assert response.json()["grants"] == {"lead": ["web-research"]}


async def test_an_unknown_capability_is_a_422_before_a_run_slot_is_taken(
    api_settings: Settings,
) -> None:
    runner = CapturingRunner()
    async with _client(api_settings, runner=runner) as client:
        response = await client.post(
            "/v1/runs", json={"prompt": "x", "grants": {"lead": ["nope"]}}
        )
    assert response.status_code == 422
    assert "known capabilities" in response.json()["detail"]
    assert runner.grants is None  # the runner was never reached


async def test_an_agent_scoped_ceiling_violation_is_a_422(api_settings: Settings) -> None:
    async with _client(api_settings, runner=CapturingRunner()) as client:
        response = await client.post(
            "/v1/runs",
            json={
                "prompt": "x",
                "team": "engineering",
                "grants": {"agents": {"reviewer": ["shell"]}},
            },
        )
    assert response.status_code == 422
    assert "reviewer" in response.json()["detail"]


async def test_a_team_scoped_ceiling_skip_is_reported_on_the_response(
    api_settings: Settings,
) -> None:
    """The difference between what was asked for and what ran, in the body."""
    async with _client(api_settings, runner=CapturingRunner()) as client:
        response = await client.post(
            "/v1/runs",
            json={"prompt": "x", "team": "engineering", "grants": {"team": ["shell"]}},
        )
    assert response.status_code == 200
    skipped = response.json()["skipped_grants"]
    assert {s["agent"] for s in skipped} >= {"reviewer"}
    assert skipped[0]["barred_tools"] == ["Bash"]


async def test_an_unknown_grant_scope_is_rejected_by_the_request_model(
    api_settings: Settings,
) -> None:
    async with _client(api_settings, runner=CapturingRunner()) as client:
        response = await client.post(
            "/v1/runs", json={"prompt": "x", "grants": {"leed": ["shell"]}}
        )
    assert response.status_code == 422


async def test_a_message_request_still_refuses_a_grants_field(api_settings: Settings) -> None:
    """Grants are fixed at session-open time, like profile and team: they were baked
    into the ClaudeAgentOptions the session connected with."""
    async with _client(api_settings, runner=CapturingRunner()) as client:
        opened = await client.post("/v1/sessions", json={})
        session_id = opened.json()["id"]
        response = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"prompt": "x", "grants": {"lead": ["shell"]}},
        )
    assert response.status_code == 422


# --- the admin surface ----------------------------------------------------------------


@pytest_asyncio.fixture
async def db_store(tmp_path: Path) -> AsyncIterator[DbCapabilityStore]:
    engine = make_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"))
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    store = DbCapabilityStore(make_session_factory(engine))
    await seed_registry(store, CAPABILITIES)
    yield store
    await dispose_engines()


async def test_mutating_routes_404_when_no_admin_key_is_configured(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    """The safe configuration is the default one, and an unconfigured admin surface
    should not advertise that it exists -- hence 404 rather than 401."""
    async with _client(
        api_settings, runner=CapturingRunner(), capability_store=db_store
    ) as client:
        response = await client.put(
            "/v1/capabilities/probe",
            json={"name": "probe", "description": "d", "tools": ["Read"]},
        )
    assert response.status_code == 404


async def test_mutating_routes_401_without_the_admin_key(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    async with _client(settings, runner=CapturingRunner(), capability_store=db_store) as client:
        response = await client.put(
            "/v1/capabilities/probe",
            json={"name": "probe", "description": "d", "tools": ["Read"]},
        )
    assert response.status_code == 401


async def test_the_run_api_key_does_not_open_the_admin_routes(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    """Authorising 'run an agent' and authorising 'change what agents may do' are
    different powers; a leak of one is a different incident from a leak of the other."""
    settings = api_settings.model_copy(
        update={"api_key": "run-key", "admin_api_key": ADMIN_KEY}
    )
    async with _client(settings, runner=CapturingRunner(), capability_store=db_store) as client:
        response = await client.put(
            "/v1/capabilities/probe",
            json={"name": "probe", "description": "d", "tools": ["Read"]},
            headers={"X-API-Key": "run-key"},
        )
    assert response.status_code == 401


async def test_an_admin_can_add_a_pack_and_it_becomes_grantable(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    runner = CapturingRunner()
    async with _client(settings, runner=runner, capability_store=db_store) as client:
        created = await client.put(
            "/v1/capabilities/notebooks",
            json={
                "name": "notebooks",
                "description": "Edit notebooks.",
                "tools": ["NotebookEdit"],
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert created.status_code == 200
        assert created.json()["revision"] == 1

        # Usable on the very next request -- the route invalidates the cache itself.
        used = await client.post(
            "/v1/runs", json={"prompt": "x", "grants": {"lead": ["notebooks"]}}
        )
    assert used.status_code == 200
    assert runner.grants is not None
    assert "NotebookEdit" in runner.grants.granted_tools()


async def test_editing_a_pack_mints_a_revision_and_history_keeps_the_old_one(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    async with _client(settings, runner=CapturingRunner(), capability_store=db_store) as client:
        headers = {"X-Admin-Key": ADMIN_KEY}
        await client.put(
            "/v1/capabilities/shell",
            json={"name": "shell", "description": "d", "tools": ["Bash", "Read"]},
            headers=headers,
        )
        history = await client.get("/v1/capabilities/shell/history")
    revisions = history.json()["revisions"]
    assert [r["revision"] for r in revisions] == [1, 2]
    assert revisions[0]["tools"] == ["Bash"]
    assert revisions[1]["tools"] == ["Bash", "Read"]


async def test_a_pack_naming_a_never_allowed_tool_is_refused_at_write_time(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    async with _client(settings, runner=CapturingRunner(), capability_store=db_store) as client:
        response = await client.put(
            "/v1/capabilities/evil",
            json={"name": "evil", "description": "d", "tools": ["KillShell"]},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
    assert response.status_code == 422


async def test_archiving_hides_a_pack_but_history_survives(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    async with _client(settings, runner=CapturingRunner(), capability_store=db_store) as client:
        headers = {"X-Admin-Key": ADMIN_KEY}
        assert (await client.delete("/v1/capabilities/shell", headers=headers)).status_code == 204
        listed = await client.get("/v1/capabilities")
        history = await client.get("/v1/capabilities/shell/history")
    assert "shell" not in {c["name"] for c in listed.json()["capabilities"]}
    assert history.status_code == 200  # run_grant rows still reference it


async def test_writing_is_a_409_when_there_is_no_database(api_settings: Settings) -> None:
    """The request is fine; the server is not configured to accept it. That is an
    operator's problem, not the caller's, so it is not a 4xx about their input."""
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    async with _client(
        settings, runner=CapturingRunner(), capability_store=SeedCapabilityStore()
    ) as client:
        response = await client.put(
            "/v1/capabilities/probe",
            json={"name": "probe", "description": "d", "tools": ["Read"]},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
    assert response.status_code == 409


async def test_a_standing_grant_applies_without_the_caller_asking(
    api_settings: Settings, db_store: DbCapabilityStore
) -> None:
    settings = api_settings.model_copy(update={"admin_api_key": ADMIN_KEY})
    runner = CapturingRunner()
    async with _client(settings, runner=runner, capability_store=db_store) as client:
        added = await client.post(
            "/v1/grants",
            json={"scope": "lead", "subject": None, "capability": "web-research"},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert added.status_code == 204
        await client.post("/v1/runs", json={"prompt": "x"})
    assert runner.grants is not None
    assert "WebSearch" in runner.grants.granted_tools()


async def test_readiness_reports_the_capability_backend(api_settings: Settings) -> None:
    async with _client(api_settings, runner=CapturingRunner()) as client:
        response = await client.get("/readyz")
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
