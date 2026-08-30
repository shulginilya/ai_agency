"""Wire contracts for the capability admin surface.

Separate module rather than more of `schemas.py`, which is already the largest file in
`api/`: these types are read and written by a different audience (an operator widening
what agents may do) than the run contracts (a client asking for work), and they are the
only ones behind the admin credential.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agency.capabilities.model import Capability, Scope


class CapabilityInfo(BaseModel):
    """One pack, at a given revision."""

    name: str
    description: str = Field(
        description="What the pack is for. This is what a human reads when approving it."
    )
    tools: list[str]
    skills: list[str]
    mcp_servers: list[str]
    prompt_note: str = Field(
        description=(
            "Appended to the grantee's prompt. A tool a worker was never told it has is "
            "a tool it will not use."
        )
    )
    revision: int = Field(
        description="1-based. 0 means this came from the in-code seed, not the database."
    )
    revision_id: int | None = Field(
        default=None,
        description=(
            "Database id of this exact content. Pinned on every run that held the pack, "
            "which is what keeps a completed run explicable after a later edit."
        ),
    )

    @classmethod
    def from_capability(cls, capability: Capability) -> CapabilityInfo:
        return cls(
            name=capability.name,
            description=capability.description,
            tools=list(capability.tools),
            skills=list(capability.skills),
            mcp_servers=list(capability.mcp_servers),
            prompt_note=capability.prompt_note,
            revision=capability.revision,
            revision_id=capability.revision_id,
        )


class CapabilitiesResponse(BaseModel):
    """Body of `GET /v1/capabilities`. An envelope, for the same reason `AgentsResponse`
    is one: a bare array cannot grow a field."""

    capabilities: list[CapabilityInfo]
    count: int
    source: str = Field(
        description=(
            "'database' or 'seed'. Worth publishing: the two are edited very differently "
            "-- one through this API, the other by shipping a commit."
        )
    )


class CapabilityHistoryResponse(BaseModel):
    """Every revision of one pack, oldest first."""

    name: str
    revisions: list[CapabilityInfo]


class CapabilityWrite(BaseModel):
    """Body of `POST`/`PUT /v1/capabilities`.

    No `revision` field: revisions are minted, never chosen. A caller that could pick one
    could overwrite history, which is the property this whole table exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = Field(
        min_length=1,
        description="Mandatory. A capability nobody can explain is one nobody should approve.",
    )
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(
        default_factory=list,
        description=(
            "Must already be loaded by this server. A pack may scope a worker to an "
            "existing MCP server; it may not introduce one."
        ),
    )
    prompt_note: str = ""

    def to_capability(self) -> Capability:
        return Capability(
            name=self.name,
            description=self.description,
            tools=tuple(self.tools),
            skills=tuple(self.skills),
            mcp_servers=tuple(self.mcp_servers),
            prompt_note=self.prompt_note,
        )


class AssignmentInfo(BaseModel):
    """One standing grant: applied to every matching run, before the caller asks."""

    scope: Scope
    subject: str | None = Field(
        description="Team name for 'team', agent name for 'agent', null for 'lead'."
    )
    capability: str


class AssignmentsResponse(BaseModel):
    assignments: list[AssignmentInfo]
    count: int


class AssignmentWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope
    subject: str | None = None
    capability: str


class ReloadResponse(BaseModel):
    """Result of dropping the registry cache."""

    reloaded: bool = True
    capabilities: int
    detail: str = Field(
        description=(
            "Cross-worker note: this reloads THIS process only. Other workers pick the "
            "change up within AGENCY_CAPABILITY_CACHE_TTL_S."
        )
    )


class SeedResponse(BaseModel):
    written: list[str] = Field(description="Packs that produced a new revision. Empty is normal.")
    unchanged: list[str]


class CapabilityDeleted(BaseModel):
    name: str
    archived_at: datetime


__all__ = [
    "AssignmentInfo",
    "AssignmentWrite",
    "AssignmentsResponse",
    "CapabilitiesResponse",
    "CapabilityDeleted",
    "CapabilityHistoryResponse",
    "CapabilityInfo",
    "CapabilityWrite",
    "ReloadResponse",
    "SeedResponse",
]
