"""The other half of the protocol-era split: a current-generation client.

`docs/00-research.md` §3 — being plainly standards-compliant is what keeps the door open for
Claude Code, hermes, or anything else, and it costs nothing extra. This is the automated form
of "point Claude Code at it": if the SDK's own client, negotiating the way a 2026-era client
negotiates, can list and call `capture`, the server is standards-clean rather than
accidentally Pebble-shaped.

Note the asymmetry with `test_handshake_legacy.py`: there we hand-roll the wire because using
a modern client would mask a legacy failure. Here the SDK client *is* the thing under test, so
using it is correct.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from signet import db

from .conftest import TOKEN


@contextlib.asynccontextmanager
async def connect(server: str) -> AsyncIterator[Client]:
    """mode='auto' is what a current client does: probe `server/discover` first, falling back
    to the initialize handshake for handshake-era servers.

    Deliberately a helper rather than a fixture: the client is anyio-based, and a pytest
    async-generator fixture can resume teardown in a different task than setup ran in, which
    trips anyio's cancel-scope check. Entering and exiting inside the test body keeps both
    ends on one task.
    """
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        transport = streamable_http_client(f"{server}/mcp", http_client=http)
        async with Client(server=transport, mode="auto") as client:
            yield client


async def test_modern_client_negotiates_the_modern_era(server: str):
    """The server must not be stuck in the ring's era.

    If this ever fails, `server/discover` stopped working and the second-client door has
    quietly closed — the ring would still work, so nothing else would notice.
    """
    async with connect(server) as client:
        assert client.protocol_version in MODERN_PROTOCOL_VERSIONS, (
            f"negotiated {client.protocol_version!r}; expected {MODERN_PROTOCOL_VERSIONS}"
        )


async def test_modern_client_lists_the_tool(server: str):
    async with connect(server) as client:
        result = await client.list_tools()
    assert [t.name for t in result.tools] == ["capture", "ask", "schedule", "do"]


async def test_modern_client_can_call_capture(server: str, cfg):
    async with connect(server) as client:
        result = await client.call_tool("capture", {"text": "from a modern client"})

    assert not result.is_error
    assert result.content[0].text == "Saved."

    conn = db.connect(cfg.db_path)
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == ["from a modern client"]
