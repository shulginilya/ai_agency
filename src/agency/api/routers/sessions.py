"""Conversations: `POST /v1/sessions` and `POST /v1/sessions/{id}/messages`.

The multi-turn counterpart to `POST /v1/runs`. A run is one prompt against a fresh
context; a session is a context that stays open, so turn three can say "now do the same
for the other file" and mean it. That is the whole reason this exists -- there is no way
to fake it by resending a transcript, because the model would re-read every tool result
instead of remembering that it already ran them.

The shape is the one `routers/runs.py` set, with one extra step. `POST /v1/runs` is
    validate -> authorise -> admit -> bound -> delegate -> map
and a message is
    validate -> authorise -> *resolve* -> serialise -> admit -> bound -> delegate -> map
where "resolve" is the session lookup (404) and "serialise" is the one-turn-at-a-time
lock (409). Both come before the run slot on purpose: they are cheap, they are specific,
and a request that is going to be refused for talking to a dead or busy session should
not first take a slot away from a request that would have succeeded.

Everything stateful lives in `api/sessions.py`; this module is the mapping to HTTP. Read
that module's docstring before changing status codes here -- the choice of 409 over a
queue, and of closing a session after a failed turn, are both reasoned there.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from agency.api.deps import SessionsDep, SettingsDep, SlotsDep, require_api_key
from agency.api.errors import (
    APIError,
    RunTimeout,
    SessionBusy,
    UpstreamError,
    raise_for_auth_failure,
)
from agency.api.schemas import (
    MessageRequest,
    MessageResponse,
    Problem,
    SessionCreateRequest,
    SessionInfo,
    SessionsResponse,
)
from agency.obs.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_key)],
    responses={
        401: {"model": Problem, "description": "Missing or invalid API key"},
        404: {"model": Problem, "description": "No such session; it was closed or expired"},
        422: {"model": Problem, "description": "Request failed validation"},
    },
)

SessionId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        description="The `id` returned by `POST /v1/sessions`.",
    ),
]


@router.post(
    "",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Open a conversation",
    response_description="The new session. Send messages to it, then close it.",
    responses={503: {"model": Problem, "description": "No session slot available; retry"}},
)
async def open_session(
    body: SessionCreateRequest,
    sessions: SessionsDep,
    settings: SettingsDep,
) -> SessionInfo:
    """Connect a session and hold it open.

    201, not 200: this creates a server-side resource -- a live SDK subprocess -- that
    the caller is now responsible for closing. `expires_at` in the body is the safety
    net if they do not.

    The profile is resolved here rather than in the store, so what comes back names the
    profile the session is actually running, not `null` meaning "whatever the server
    was configured with at the time".
    """
    profile = body.profile or settings.profile
    session = await sessions.open(profile=profile, team=body.team)
    return SessionInfo.from_session(session)


@router.get(
    "",
    response_model=SessionsResponse,
    summary="List open sessions",
    response_description="Every session this process currently holds open.",
)
async def list_sessions(sessions: SessionsDep) -> SessionsResponse:
    """What this process is holding open, oldest first.

    Per-process, like `GET /v1/agents`' status: sessions are local subprocesses, so with
    more than one uvicorn worker each worker lists only its own. It is an operational
    view -- "what is this box holding" -- not a user's conversation history.
    """
    return SessionsResponse.from_sessions(await sessions.list())


@router.get(
    "/{session_id}",
    response_model=SessionInfo,
    summary="Retrieve one session",
    response_description="The session's status, turn count and expiry.",
)
async def get_session(session_id: SessionId, sessions: SessionsDep) -> SessionInfo:
    """Poll one session. `status` is what a client waits on before sending a turn, and
    a 404 is the honest answer for a handle that has expired -- see `SessionStore.get`."""
    return SessionInfo.from_session(await sessions.get(session_id))


@router.post(
    "/{session_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Send one message and get the reply",
    response_description="The completed turn, including its tool and delegation trace.",
    responses={
        409: {"model": Problem, "description": "A turn is already in flight on this session"},
        502: {"model": Problem, "description": "The agent loop failed; the session was closed"},
        503: {"model": Problem, "description": "No run slot available; retry"},
        504: {"model": Problem, "description": "Turn exceeded the server timeout"},
    },
)
async def send_message(
    session_id: SessionId,
    body: MessageRequest,
    sessions: SessionsDep,
    slots: SlotsDep,
    settings: SettingsDep,
) -> MessageResponse:
    """One turn of the conversation.

    A turn takes a `RunSlots` slot as well as the session lock. An open session is idle
    weight -- a subprocess and some memory -- but a *running* turn is exactly as
    expensive as a `POST /v1/runs`, so the two endpoints share one bound on concurrent
    model work. Without that, N sessions would be N ways around the run cap.

    `is_error=True` inside `run` is still a 200, same contract as `POST /v1/runs`. What
    differs is the failure handling: a timeout or a transport error cancels the drain
    mid-stream and leaves the SDK client in an indeterminate state, so `SessionStore`
    closes the session on the way out. The 504 and 502 bodies say so, because "retry the
    same session id" would otherwise be the obvious and wrong next move.
    """
    session = await sessions.get(session_id)
    try:
        async with sessions.turn(session), slots.slot():
            log.info("api.session.message", session_id=session.id, turn=session.turns + 1)
            record = await asyncio.wait_for(
                session.conversation.send(body.prompt),
                timeout=settings.api_run_timeout_s,
            )
            # Before the counter: a turn that could not authenticate never reached the
            # model, so counting it would overstate the conversation. Raised *inside*
            # the turn block on purpose -- `SessionStore.turn` lets an `APIError` through
            # without closing the session, which is what we want here: the context is
            # intact and the session works again the moment the server has credentials.
            raise_for_auth_failure(record)
            session.turns += 1
    except APIError:
        # Either refused before the turn ran (busy session, saturated run pool) or
        # refused after it on credentials. Neither touched the conversation, so the
        # session stays open -- do not let these fall into the handlers below.
        raise
    except asyncio.TimeoutError:
        log.warning(
            "api.session.timeout",
            session_id=session_id,
            timeout_s=settings.api_run_timeout_s,
        )
        raise RunTimeout(
            f"The turn exceeded the {settings.api_run_timeout_s:.0f}s server timeout. "
            "The session was closed, because a cancelled turn leaves its context "
            "half-written; open a new one."
        ) from None
    except Exception as exc:  # noqa: BLE001 - mapped to 502, re-logged with traceback
        log.exception("api.session.failed", session_id=session_id, error=str(exc))
        raise UpstreamError(
            f"The agent loop failed: {type(exc).__name__}. The session was closed."
        ) from exc

    log.info(
        "api.session.message.done",
        session_id=session.id,
        turn=session.turns,
        run_id=record.run_id,
        is_error=record.is_error,
    )
    return MessageResponse.from_record(session, record)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Close a conversation",
    responses={
        409: {"model": Problem, "description": "A turn is in flight; wait for it to finish"},
    },
)
async def close_session(session_id: SessionId, sessions: SessionsDep) -> Response:
    """Release the subprocess and the context.

    Refuses while a turn is in flight rather than tearing the client out from under it:
    that would surface to the caller mid-turn as a 502 with no explanation. The wait is
    bounded by the turn's own timeout, and a client that has given up entirely does not
    need this endpoint at all -- the idle reaper collects the session either way.

    204 and idempotent-ish: a `DELETE` of an id that has already been closed or reaped
    is a 404 from `sessions.get`, which is the truthful answer to "is this handle live".
    """
    session = await sessions.get(session_id)
    if session.busy:
        raise SessionBusy(
            f"Session {session.id!r} is handling a message. Wait for the reply, then close it."
        )
    await sessions.close(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
