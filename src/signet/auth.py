"""Bearer auth for the MCP surface.

The Pebble app can send exactly one thing: a single `Authorization` header value, typed by
hand and used verbatim (`docs/00-research.md` §2). No OAuth, no DCR, no Cloudflare Access
service tokens — those need two custom headers the app cannot send. So: one long random
bearer, compared in constant time.

P1 replaces the single env token with the hashed `tokens` table and scopes; the interface
here (`Principal`) is the seam that makes that a swap rather than a rewrite.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Principal:
    """Who is calling. P1 fills in scopes from the tokens table."""

    client_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)


class StaticTokenVerifier:
    """P0: exactly one valid token, from the environment."""

    def __init__(self, token: str, client_id: str = "ring") -> None:
        self._token = token
        self._client_id = client_id

    def verify(self, presented: str) -> Principal | None:
        if secrets.compare_digest(presented, self._token):
            return Principal(client_id=self._client_id)
        return None


def extract_bearer(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == b"authorization":
            raw = value.decode("latin-1").strip()
            scheme, _, token = raw.partition(" ")
            if scheme.lower() != "bearer":
                return None
            return token.strip()
    return None


async def _json_401(send: Send) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": "Unauthorized"},
            "id": None,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="signet"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Guards `protected_prefix` only. `/healthz` stays open so Docker and uptime-kuma
    can probe it without holding a credential.

    Pure ASGI rather than BaseHTTPMiddleware: BaseHTTPMiddleware wraps responses in a way
    that interferes with long-lived streaming bodies, and the MCP transport owns its own
    response lifecycle.
    """

    def __init__(
        self,
        app: ASGIApp,
        verifier: StaticTokenVerifier,
        protected_prefix: str = "/mcp",
        on_reject: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.protected_prefix = protected_prefix
        self.on_reject = on_reject

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.protected_prefix):
            await self.app(scope, receive, send)
            return

        presented = extract_bearer(scope.get("headers", []))
        principal = self.verifier.verify(presented) if presented else None
        if principal is None:
            if self.on_reject:
                await self.on_reject(scope.get("client", ("?", 0))[0])
            await _json_401(send)
            return

        scope["signet.principal"] = principal
        await self.app(scope, receive, send)
