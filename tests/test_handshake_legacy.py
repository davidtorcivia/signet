"""The 2025-06-18 handshake, driven by hand.

The Pebble app ships MCP Kotlin SDK 0.8.3, whose `LATEST_PROTOCOL_VERSION` is 2025-06-18
(`docs/00-research.md` §2). Current SDKs speak 2026-07-28, in which the whole
initialize/initialized handshake no longer exists. This file is the proof that signet still
serves the old era.

It deliberately does **not** use the SDK's own client: a modern client negotiates a modern
version, which would make this test pass while the ring fails. Raw JSON-RPC only.
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import TOKEN

PEBBLE_PROTOCOL_VERSION = "2025-06-18"

# What the Kotlin client sends: it accepts both, and the transport picks. json_response=True
# is what makes the server answer with plain JSON anyway — which is the point of the
# Content-Type assertions below.
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc(method: str, params: dict | None = None, id_: int | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return body


def assert_json_not_sse(response: httpx.Response) -> None:
    """The single most load-bearing assertion in the repo.

    Cloudflare Tunnel buffers SSE until the stream closes (`docs/00-research.md` §5.1), so an
    SSE-framed response is a response the ring never receives. The SDK's transport defaults to
    SSE framing; `json_response=True` turns it off. If this assertion ever fails, the deployed
    server will hang behind the tunnel while every local test still passes.
    """
    ctype = response.headers.get("content-type", "")
    assert "text/event-stream" not in ctype, f"SSE framing is back on: {ctype!r}"
    assert "application/json" in ctype, f"expected JSON, got {ctype!r}"


class LegacyClient:
    """A hand-rolled Pebble-shaped client."""

    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=15)
        self.session_id: str | None = None

    def post(self, body: dict) -> httpx.Response:
        headers = dict(HEADERS)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return self.http.post("/mcp", json=body, headers=headers)

    def initialize(self) -> dict:
        response = self.post(
            _rpc(
                "initialize",
                {
                    "protocolVersion": PEBBLE_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pebble-sim", "version": "0.8.3"},
                },
                id_=1,
            )
        )
        assert response.status_code == 200, response.text
        assert_json_not_sse(response)
        self.session_id = response.headers.get("mcp-session-id")
        return response.json()

    def initialized(self) -> httpx.Response:
        return self.post(_rpc("notifications/initialized"))

    def call(self, method: str, params: dict | None = None, id_: int = 2) -> dict:
        response = self.post(_rpc(method, params, id_=id_))
        assert response.status_code == 200, response.text
        assert_json_not_sse(response)
        return response.json()


@pytest.fixture
def legacy(server: str):
    client = LegacyClient(server)
    yield client
    client.http.close()


def test_initialize_negotiates_pebble_era_version(legacy: LegacyClient):
    result = legacy.initialize()["result"]
    assert result["protocolVersion"] == PEBBLE_PROTOCOL_VERSION, (
        "server upgraded the client past its ceiling; the ring cannot follow"
    )
    assert result["serverInfo"]["name"] == "signet"


def test_initialize_carries_instructions(legacy: LegacyClient):
    # The app injects these into the on-device model's context (getExtraContext()).
    result = legacy.initialize()["result"]
    assert result.get("instructions"), "instructions is a free system-prompt channel; use it"


def test_tools_list_is_one_page_with_no_cursor(legacy: LegacyClient):
    legacy.initialize()
    legacy.initialized()
    result = legacy.call("tools/list")["result"]

    assert "nextCursor" not in result, (
        'the Kotlin client hits TODO("Handle pagination") and throws on nextCursor'
    )
    names = [t["name"] for t in result["tools"]]
    assert names == ["capture", "ask", "schedule", "do"]
    # A tiny on-device model picks from this list. Research section 5.4: keep it under about
    # eight tools on principle, and capture first because it is the one that must never fail.
    assert len(names) <= 8
    schema = result["tools"][0]["inputSchema"]
    assert schema["required"] == ["text"]


def test_capture_returns_core_schema_shape(legacy: LegacyClient, cfg):
    legacy.initialize()
    legacy.initialized()
    result = legacy.call(
        "tools/call", {"name": "capture", "arguments": {"text": "handshake test"}}, id_=3
    )["result"]

    # Verified against ToolCallResult.kt: the app reads _meta.coreSchema as an int, then
    # getValue()s BOTH "output" and "semanticResult" — a missing key throws on the phone.
    assert result["_meta"]["coreSchema"] == 1
    assert isinstance(result["_meta"]["coreSchema"], int)
    structured = result["structuredContent"]
    assert structured["output"] == "Saved."
    assert structured["semanticResult"] == {"type": "Response", "text": "Saved."}
    # Plain-text clients must still get something.
    assert result["content"][0]["text"] == "Saved."

    from signet import db

    conn = db.connect(cfg.db_path)
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == ["handshake test"]


def test_semantic_result_carries_no_unknown_keys(legacy: LegacyClient):
    """kotlinx.serialization's default Json does not ignore unknown keys.

    The app decodes semanticResult with a plain `Json.decodeFromJsonElement`, so any field
    not on the Kotlin data class raises there rather than here. Response is (text, question?).
    """
    legacy.initialize()
    legacy.initialized()
    result = legacy.call(
        "tools/call", {"name": "capture", "arguments": {"text": "field check"}}, id_=4
    )["result"]
    assert set(result["structuredContent"]["semanticResult"]) <= {"type", "text", "question"}
