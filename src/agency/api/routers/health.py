"""Liveness and readiness.

Split on purpose, because they answer different questions and have different
consequences:

- `/healthz` — "is this process alive?" No dependencies, no auth, no I/O. A failure
  here means the orchestrator should restart the container.
- `/readyz` — "should this instance receive traffic?" Checks what a run actually needs.
  A failure here means take it out of the load balancer; it may well recover.

Conflating them is how a slow dependency turns into a restart loop.
"""

from __future__ import annotations

from fastapi import APIRouter

from agency.api.deps import CapabilitiesDep, SessionsDep, SettingsDep, SlotsDep
from agency.api.schemas import Health, Readiness
from agency.api.version import VERSION

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=Health, summary="Liveness probe")
async def healthz() -> Health:
    return Health(version=VERSION)


@router.get("/readyz", response_model=Readiness, summary="Readiness probe")
async def readyz(
    settings: SettingsDep,
    slots: SlotsDep,
    sessions: SessionsDep,
    capabilities: CapabilitiesDep,
) -> Readiness:
    checks: dict[str, str] = {}

    for name, path in (
        ("workspace", settings.workspace_dir),
        ("run_dir", settings.run_dir),
    ):
        try:
            resolved = path.resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            probe = resolved / ".readyz"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            checks[name] = "ok"
        except OSError as exc:
            checks[name] = f"not writable: {exc.strerror or exc}"

    # Not "is a slot free" -- a fully-busy instance is healthy, just saturated, and
    # pulling it out of rotation while it works would be exactly wrong.
    checks["capacity"] = "ok" if slots.limit > 0 else "no slots configured"

    # Readiness, never liveness. This module's own docstring makes the argument, and the
    # database is exactly the dependency it was written for: a blip should take this
    # instance out of rotation, not restart the container.
    #
    # The probe is a registry read rather than a bare `SELECT 1`, because that is what a
    # run actually needs -- a reachable server whose capability tables answer. With no
    # database configured this reads the in-code seed and always succeeds, which is
    # correct: nothing is missing.
    backend = settings.database_backend()
    try:
        count = len(await capabilities.registry())
        checks["database"] = "ok" if backend == "none" else f"ok ({backend}, {count} packs)"
    except Exception as exc:  # noqa: BLE001 - the point is to report, not to raise
        checks["database"] = f"unreachable: {exc}"

    status = "ready" if all(v.startswith("ok") for v in checks.values()) else "degraded"
    return Readiness(
        status=status,  # type: ignore[arg-type]
        checks=checks,
        capacity={
            "limit": slots.limit,
            "in_flight": slots.in_flight,
            # Sessions are held open between requests, so "how many are parked here" is
            # the number an operator needs before draining or restarting this instance.
            "session_limit": sessions.limit,
            "sessions_open": sessions.count,
        },
    )
