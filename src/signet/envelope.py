"""The normalized shapes that flow between layers.

Every inlet (MCP tool call, webhook, cron, the web app) turns its input into a `Request`, and
every capability returns an `Outcome`. Nothing downstream of an inlet knows or cares where the
text came from, which is what makes adding an inlet cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .auth import Principal


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Request:
    """One thing the user asked for, however it arrived."""

    text: str
    source: str = "api"  # mcp:ring | webhook | cron | web | api
    id: str = ""
    received_at: str = field(default_factory=_now)
    verb: str | None = None  # capture | ask | schedule | do
    client: Principal | None = None
    audio_path: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)
    reply_to: list[str] = field(default_factory=lambda: ["mcp"])

    @property
    def scopes(self) -> frozenset[str]:
        return self.client.scopes if self.client else frozenset()

    @property
    def client_id(self) -> int | None:
        return self.client.token_id if self.client else None


@dataclass
class Outcome:
    """What a capability produced.

    Three audiences, deliberately separated:

    - `output` is the string handed to the on-device model. It can be as long as it needs to be.
    - `semantic` is the Pebble semanticResult. This is what renders in the feed and what
      surfaces on the watch, so it should be short and in the right variant.
    - `data` is the structured payload for internal callers, such as the agent loop chaining
      one capability into the next.
    """

    output: str
    semantic: dict[str, Any]
    data: Any = None
    is_error: bool = False
    untrusted: bool = False
    """True when this result contains text fetched from outside, such as web pages. Anything
    derived from it must stay fenced, and must not be allowed to trigger further capabilities
    without approval. Prompt injection is the reason."""
    error: str | None = None
    """Why something went wrong, for the feed. Never shown to the ring: the watch gets the
    plain sentence in `semantic`, and this is for the person debugging afterwards."""
    cost_usd: float = 0.0
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
