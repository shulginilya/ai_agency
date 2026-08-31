"""The demo endpoint: `POST /v1/runs`.

One real agent run, start to finish, through the same `runner.run_once` the CLI uses.
It is the template for every future endpoint -- copy its shape:

    validate (schemas) -> authorise (dependency) -> admit (slot) -> bound (timeout)
    -> delegate (Runner seam) -> map to a wire model

Note what it does *not* do: build `ClaudeAgentOptions`, touch the SDK, or read env.
`options.build_options` stays the single chokepoint, and the HTTP layer stays a thin
adapter over the same code path the CLI exercises.

Synchronous by design at this size: the caller holds the connection until the run ends.
That is fine for runs measured in seconds and wrong for runs measured in minutes -- at
that point return `202` with a job id and move the work to a queue. The `RunSlots` and
timeout wiring here is what makes that migration a change of transport rather than a
rewrite.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, status

from agency.api.deps import (
    CapabilitiesDep,
    RunnerDep,
    SettingsDep,
    SlotsDep,
    require_api_key,
)
from agency.api.errors import RunTimeout, UpstreamError, raise_for_auth_failure
from agency.api.schemas import PROBLEM_RESPONSES, Problem, RunRequest, RunResponse
from agency.capabilities.model import Grants, ResolvedGrants
from agency.obs.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    dependencies=[Depends(require_api_key)],
    responses={
        **PROBLEM_RESPONSES,
        502: {"model": Problem, "description": "The agent loop failed"},
        503: {"model": Problem, "description": "No run slot available; retry"},
        504: {"model": Problem, "description": "Run exceeded the server timeout"},
    },
)


async def _resolve(body: RunRequest, capabilities: CapabilitiesDep) -> ResolvedGrants:
    """Standing assignments plus what the caller asked for, pinned to revisions.

    Deferred import of `runner`, matching the rule that nothing under `api/` may import
    `agency.runner` at module scope -- `deps.make_runner` is the only binding to the real
    loop, and this keeps that true.
    """
    from agency.runner import resolve_grants

    requested = body.grants.to_grants() if body.grants else Grants()
    return await resolve_grants(capabilities, requested, team=body.team)


@router.post(
    "",
    response_model=RunResponse,
    status_code=status.HTTP_200_OK,
    summary="Run one prompt to completion",
    response_description="The completed run, including its tool and delegation trace.",
)
async def create_run(
    body: RunRequest,
    runner: RunnerDep,
    slots: SlotsDep,
    settings: SettingsDep,
    capabilities: CapabilitiesDep,
) -> RunResponse:
    # Resolved before a slot is taken, deliberately: an unknown pack or a ceiling
    # violation must fail before this request occupies one of a small number of run
    # slots, and long before a subprocess is spawned. `GrantError` subclasses ValueError
    # and the handler maps it to a 422.
    grants = await _resolve(body, capabilities)

    async with slots.slot():
        log.info(
            "api.run.start",
            profile=body.profile or settings.profile,
            team=body.team,
            grants=grants.summary(),
        )
        try:
            with anyio.fail_after(settings.api_run_timeout_s):
                record = await runner(
                    body.prompt, profile=body.profile, team=body.team, grants=grants
                )
        except TimeoutError:
            # The timeout unwinds `Agent.__aexit__`, which tears down the SDK
            # subprocess; without that the process is orphaned and keeps billing after
            # the client has given up.
            #
            # It has to be `anyio.fail_after` for that to be true, though this file
            # asserted it while using `asyncio.wait_for`, which does the opposite.
            # `wait_for` cancels a wrapper task -- a raw asyncio cancellation -- and
            # `SubprocessCLITransport.close()` documents that its shield only defers
            # cancellation raised by an anyio cancel scope. A raw one lands at the first
            # await of the teardown and skips the terminate -> kill escalation, leaving
            # exactly the orphan the comment claimed to prevent. `TimeoutError` is the
            # builtin for the same reason it is in routers/sessions.py.
            log.warning("api.run.timeout", timeout_s=settings.api_run_timeout_s)
            raise RunTimeout(
                f"The run exceeded the {settings.api_run_timeout_s:.0f}s server timeout "
                "and was cancelled."
            ) from None
        except Exception as exc:  # noqa: BLE001 - mapped to 502, re-logged with traceback
            log.exception("api.run.failed", error=str(exc))
            raise UpstreamError(f"The agent loop failed: {type(exc).__name__}") from exc

    # ...with one exception, checked first: a run that could not authenticate never
    # reached the model, so it is "we could not run it" wearing a model failure's
    # clothes. 502 with its own type URI, not a 200 whose body happens to say
    # "OAuth session expired".
    raise_for_auth_failure(record)

    # An agent that errored is a successful request with is_error=True. Reserve 5xx for
    # "we could not run it", so a client can tell "the model failed" from "we failed".
    log.info("api.run.done", run_id=record.run_id, is_error=record.is_error)
    return RunResponse.from_record(record, grants)
