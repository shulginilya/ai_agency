"""Route modules.

One module per resource. `health` is unversioned and unauthenticated (probes are
infrastructure, not API consumers); everything else hangs off the `/v1` prefix so a
breaking change can ship as `/v2` alongside it rather than in place of it.
"""

from __future__ import annotations

from agency.api.routers import agents, health, runs

__all__ = ["agents", "health", "runs"]
