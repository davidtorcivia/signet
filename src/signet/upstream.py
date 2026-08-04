"""Remote MCP servers mounted as internal capabilities.

The routing station, made real. Point signet at any MCP server and its tools become things the
agent can use, with no code written per integration.

Three properties make this safe rather than merely convenient:

- **The ring's tool list never grows.** Upstream tools mount as internal capabilities, so they
  are reachable through `do` and invisible to the on-device model that has to choose between
  four verbs. Tool-count pressure, the constraint that shapes the whole design, does not apply.
- **Credentials stay here.** The phone can hold a handful of hand-typed servers with hand-typed
  tokens. Through signet that collapses to one URL and one token.
- **Nothing is trusted by default.** A new server's tools require approval and its results are
  treated as untrusted until told otherwise, because signet cannot know from a schema whether
  a tool reads a door sensor or opens the door.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from . import coreschema, db
from .capability import Capability
from .envelope import Outcome, Request

logger = logging.getLogger("signet.upstream")

CALL_TIMEOUT = 30.0

# Verbs that only read. Everything else needs approval, because the difference between
# get_state and lock_door is not something a schema reveals, and guessing wrong once is worse
# than a tap.
READ_VERBS = frozenset(
    [
        "browse",
        "check",
        "describe",
        "fetch",
        "find",
        "get",
        "list",
        "lookup",
        "query",
        "read",
        "search",
        "show",
        "status",
        "summarise",
        "summarize",
        "view",
    ]
)

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def _leading_verb(tool_name: str) -> str:
    """The first word of a tool name, however it was written.

    Matching on a word boundary does not work here: an underscore counts as a word character,
    so `get_state` never matched a `get\\b` pattern and every reader was treated as an action.
    """
    tail = tool_name.split(".")[-1]
    return _CAMEL.sub("_", tail).lower().strip("_").split("_")[0]


def looks_read_only(tool_name: str) -> bool:
    return _leading_verb(tool_name) in READ_VERBS


class PassthroughArgs(BaseModel):
    """Arguments are validated by the upstream server, which owns the schema.

    Rebuilding a JSON Schema as a pydantic model here would be a compiler with its own bugs,
    and a wrong rejection is worse than a clean error from the server that actually knows.
    """

    model_config = ConfigDict(extra="allow")


@dataclass
class Upstream:
    name: str
    url: str
    auth_header: str | None = None
    trusted: bool = False
    approve_all: bool = False
    allow_tools: list[str] | None = None

    @property
    def scope(self) -> str:
        """One scope per server, so revoking a token's access to your house is one tick."""
        return f"mcp:{self.name}"


def from_row(row: sqlite3.Row) -> Upstream:
    return Upstream(
        name=row["name"],
        url=row["url"],
        auth_header=row["auth_header"],
        trusted=bool(row["trusted"]),
        approve_all=bool(row["approve_all"]),
        allow_tools=json.loads(row["allow_tools"]) if row["allow_tools"] else None,
    )


def list_upstreams(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[sqlite3.Row]:
    clause = " WHERE enabled = 1" if enabled_only else ""
    return list(conn.execute(f"SELECT * FROM upstreams{clause} ORDER BY name"))


def get_upstream(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM upstreams WHERE name = ?", (name,)).fetchone()


def save_upstream(
    conn: sqlite3.Connection,
    *,
    name: str,
    url: str,
    auth_header: str | None,
    trusted: bool = False,
    approve_all: bool = False,
    enabled: bool = True,
) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO upstreams(name, url, auth_header, trusted, approve_all, enabled, "
            "created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET url=excluded.url, "
            # A blank header on edit means keep the existing one, the same rule every other
            # secret in the portal follows.
            "auth_header=COALESCE(NULLIF(excluded.auth_header, ''), upstreams.auth_header), "
            "trusted=excluded.trusted, approve_all=excluded.approve_all, "
            "enabled=excluded.enabled",
            (name, url, auth_header, int(trusted), int(approve_all), int(enabled), db.now_iso()),
        )


def delete_upstream(conn: sqlite3.Connection, name: str) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM upstreams WHERE name = ?", (name,))


def record_tools(
    conn: sqlite3.Connection, name: str, tools: list[dict], error: str | None = None
) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE upstreams SET tools_json = ?, last_seen_at = ?, last_error = ? WHERE name = ?",
            (json.dumps(tools), db.now_iso(), error, name),
        )


def record_failure(conn: sqlite3.Connection, name: str, error: str) -> None:
    """Keeps whatever tools were last known. A server being down should not silently remove
    capabilities that will come back in a minute."""
    with db.transaction(conn):
        conn.execute("UPDATE upstreams SET last_error = ? WHERE name = ?", (error[:300], name))


def cached_tools(row: sqlite3.Row) -> list[dict]:
    try:
        return json.loads(row["tools_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def _client(upstream: Upstream):
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": upstream.auth_header} if upstream.auth_header else {}
    http = httpx2.AsyncClient(headers=headers, timeout=CALL_TIMEOUT)
    return http, Client, streamable_http_client


async def fetch_tools(upstream: Upstream) -> list[dict]:
    """Ask a server what it can do. Raises, so the caller can record why it failed."""
    http, Client, streamable_http_client = _client(upstream)
    async with http:
        transport = streamable_http_client(upstream.url, http_client=http)
        async with Client(server=transport, mode="auto") as client:
            result = await client.list_tools()

    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema or {"type": "object", "properties": {}},
        }
        for tool in result.tools
    ]


async def call_tool(upstream: Upstream, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Returns the text the tool produced and whether it reported an error."""
    http, Client, streamable_http_client = _client(upstream)
    async with http:
        transport = streamable_http_client(upstream.url, http_client=http)
        async with Client(server=transport, mode="auto") as client:
            result = await client.call_tool(tool, args)

    parts = [
        getattr(block, "text", "") for block in (result.content or []) if getattr(block, "text", "")
    ]
    return "\n".join(parts).strip() or "Done.", bool(result.is_error)


def build_capability(upstream: Upstream, tool: dict) -> Capability:
    name = f"{upstream.name}.{tool['name']}"
    destructive = upstream.approve_all or not looks_read_only(tool["name"])

    async def handler(request: Request, args: PassthroughArgs) -> Outcome:
        payload = args.model_dump(mode="json", exclude_none=True)
        try:
            text, failed = await call_tool(upstream, tool["name"], payload)
        except Exception as exc:
            logger.warning("%s failed: %s", name, exc)
            message = f"{upstream.name} did not answer"
            return Outcome(
                output=message,
                semantic=coreschema.action_logged(upstream.name, message, success=False),
                is_error=True,
                error=str(exc)[:300],
            )

        headline = text.splitlines()[0][:140] if text else "Done."
        return Outcome(
            output=text,
            semantic=coreschema.action_logged(upstream.name, headline, success=not failed),
            is_error=failed,
        )

    return Capability(
        name=name,
        description=tool["description"] or f"{tool['name']} on {upstream.name}",
        schema=PassthroughArgs,
        handler=handler,
        scopes=(upstream.scope,),
        exposure="internal",
        tier="fast",
        destructive=destructive,
        # Trust is per server. Home Assistant's output is yours; a scraping server's is not,
        # and an untrusted result narrows the agent loop to reading for the rest of the run.
        returns_untrusted=not upstream.trusted,
        # The server's own schema, so the agent sees the arguments it actually wants.
        metadata={"input_schema": tool["input_schema"], "upstream": upstream.name},
    )


def mount(registry, conn: sqlite3.Connection) -> list[str]:
    """Register every enabled upstream's known tools.

    From cache rather than live: booting must not depend on someone else's server being up,
    and a home automation box that is rebooting should not take signet's capabilities with it.
    """
    mounted: list[str] = []
    for row in list_upstreams(conn, enabled_only=True):
        upstream = from_row(row)
        for tool in cached_tools(row):
            if upstream.allow_tools and tool["name"] not in upstream.allow_tools:
                continue
            capability = build_capability(upstream, tool)
            if registry.get(capability.name) is not None:
                continue
            registry.register(capability)
            mounted.append(capability.name)
    if mounted:
        logger.info("mounted %d upstream tools: %s", len(mounted), ", ".join(mounted[:8]))
    return mounted


def known_scopes(conn: sqlite3.Connection) -> list[str]:
    """Every scope a token could be granted, including one per mounted server."""
    from .auth import ALL_SCOPES

    return sorted({*ALL_SCOPES, *(f"mcp:{row['name']}" for row in list_upstreams(conn))})
