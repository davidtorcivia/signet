"""The capability contract.

A capability is the pluggable unit and the whole point of the design. Declaring one should be
the only thing you do: the registry handles scope checks, rate limits, the kill switch, the
approval queue, audit logging, and MCP exposure.

If adding a capability ever requires editing the router, the registry, or the MCP layer, the
seam is in the wrong place. `tests/test_registry.py` asserts that directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from .envelope import Outcome, Request

Exposure = Literal["internal", "mcp", "both"]
Tier = Literal["instant", "fast", "slow"]

Handler = Callable[[Request, BaseModel], Awaitable[Outcome]]


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Handler

    scopes: tuple[str, ...] = ()
    """What the caller must hold. A capability whose scopes are unmet is invisible in
    tools/list and rejected if called anyway."""

    exposure: Exposure = "internal"
    """Keeps the ring's tool list short while the internal toolbelt grows without limit. Only
    `mcp` and `both` appear in tools/list, filtered further by the caller's scopes."""

    tier: Tier = "fast"
    """instant: answer now. fast: answer within the sync budget. slow: queue it and push the
    result later. Decides sync vs async so no capability author has to think about the edge
    timeout."""

    destructive: bool = False
    """Unlocking doors, deleting things, spending money. These queue for approval instead of
    running, because the ring cannot answer a confirmation prompt."""

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def mcp_visible(self) -> bool:
        return self.exposure in ("mcp", "both")

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the MCP tool definition."""
        schema = self.schema.model_json_schema()
        schema.pop("title", None)
        return schema

    def permitted_for(self, scopes: frozenset[str]) -> bool:
        return all(scope in scopes for scope in self.scopes)
