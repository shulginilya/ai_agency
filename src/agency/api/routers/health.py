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

from agency.api.deps import SettingsDep, SlotsDep
from agency.api.schemas import Health, Readiness
from agency.api.version import VERSION

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=Health, summary="Liveness probe")
async def healthz() -> Health:
    return Health(version=VERSION)


@router.get("/readyz", response_model=Readiness, summary="Readiness probe")
async def readyz(settings: SettingsDep, slots: SlotsDep) -> Readiness:
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

    status = "ready" if all(v == "ok" for v in checks.values()) else "degraded"
    return Readiness(
        status=status,  # type: ignore[arg-type]
        checks=checks,
        capacity={"limit": slots.limit, "in_flight": slots.in_flight},
    )
