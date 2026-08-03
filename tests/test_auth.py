"""Auth and the JSON-never-HTML invariant.

The token sits in a phone app and cannot be rotated from there, so the failure modes that
matter are: it must not be guessable, it must not leak through an error page, and the health
probe must not need it.
"""

from __future__ import annotations

import httpx

from .conftest import TOKEN

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pebble-sim", "version": "0.8.3"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_healthz_needs_no_token(server: str):
    response = httpx.get(f"{server}/healthz", timeout=10)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_missing_token_is_json_401(server: str):
    response = httpx.post(f"{server}/mcp", json=INIT, headers=ACCEPT, timeout=10)
    assert response.status_code == 401
    # hermes-agent raises NonMcpEndpointError on text/html; more importantly an HTML error
    # page from this endpoint means something is mounted wrong.
    assert "application/json" in response.headers["content-type"]
    assert response.json()["error"]["message"] == "Unauthorized"


def test_wrong_token_is_rejected(server: str):
    response = httpx.post(
        f"{server}/mcp",
        json=INIT,
        headers={**ACCEPT, "Authorization": f"Bearer {TOKEN}x"},
        timeout=10,
    )
    assert response.status_code == 401


def test_token_without_bearer_scheme_is_rejected(server: str):
    """The app uses authHeader verbatim, so a user who types the bare token gets 401 —
    that is the correct outcome, and this test documents it for the setup instructions."""
    response = httpx.post(
        f"{server}/mcp", json=INIT, headers={**ACCEPT, "Authorization": TOKEN}, timeout=10
    )
    assert response.status_code == 401


def test_valid_token_passes(server: str):
    response = httpx.post(
        f"{server}/mcp",
        json=INIT,
        headers={**ACCEPT, "Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    assert response.status_code == 200
