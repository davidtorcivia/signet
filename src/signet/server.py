"""ASGI wiring: the MCP surface at /mcp, an unauthenticated /healthz, bearer auth in front.

P0 scope: one tool, `capture`. Everything else in the design (`docs/02-architecture.md`) —
envelope, router, registry, capabilities — arrives in P1 behind this same surface.
"""

from __future__ import annotations

import logging

import mcp.types as types
from mcp.server.lowlevel import Server
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import coreschema, db
from .auth import BearerAuthMiddleware, StaticTokenVerifier
from .config import Config

logger = logging.getLogger("signet")

# Injected into the on-device model's context via `initialize` (the client returns
# `serverInstructions` from `getExtraContext()`). A free system-prompt channel —
# `docs/00-research.md` §2. Keep it short: it is prepended to a ~1B model's context.
INSTRUCTIONS = (
    "signet stores what you say. Prefer the capture tool for anything the user wants "
    "remembered, noted, or written down."
)

# One required string field. The caller is a ~1B on-device tool-calling model; every
# optional field is another way for it to produce something unusable.
CAPTURE_TOOL = types.Tool(
    name="capture",
    description=(
        "Save a note, thought, or reminder exactly as spoken. "
        "Use when the user wants something remembered or written down."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The note to save, in the user's own words.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)


def build_mcp_server(cfg: Config) -> Server:
    async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        # No nextCursor, ever. The Pebble client hits `TODO("Handle pagination")` and
        # throws if one is present (`docs/00-research.md` §2). Keep the list to one page.
        return types.ListToolsResult(tools=[CAPTURE_TOOL])

    async def on_call_tool(
        ctx: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name != "capture":
            return coreschema.result(
                f"Unknown tool: {params.name}",
                coreschema.generic_failure(
                    f"signet has no tool called {params.name}.", llm_recoverable=False
                ),
                is_error=True,
            )

        args = params.arguments or {}
        text = (args.get("text") or "").strip()
        if not text:
            return coreschema.result(
                "Nothing to save, no text was provided.",
                coreschema.generic_failure("Nothing to save.", llm_recoverable=True),
                is_error=True,
            )

        conn = db.connect(cfg.db_path)
        try:
            request_id = db.start_request(conn, text=text, source="mcp:ring", verb="capture")
            entry_id = db.add_journal(conn, text, request_id=request_id)
            db.finish_request(conn, request_id, status="ok", result={"journal_id": entry_id})
        finally:
            conn.close()

        logger.info("capture id=%s chars=%d", entry_id, len(text))
        return coreschema.result("Saved.", coreschema.response("Saved."))

    return Server(
        "signet",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "signet"})


def init_storage(cfg: Config) -> None:
    """Migrate, register the env token as a row, and absorb any P0 journal file.

    Runs at boot rather than lazily, so a broken schema stops the container instead of
    surfacing on the first ring press.
    """
    conn = db.connect(cfg.db_path)
    try:
        applied = db.migrate(conn)
        if applied:
            logger.info("applied migrations: %s", ", ".join(applied))
        db.seed_token(conn, cfg.token)
        imported = db.import_legacy_journal(conn, cfg.journal_path)
        if imported:
            logger.info("imported %d captures from the P0 journal file", imported)
    finally:
        conn.close()


def create_app(cfg: Config | None = None):
    from . import config as config_module

    cfg = cfg or config_module.load()
    init_storage(cfg)
    server = build_mcp_server(cfg)

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        # The transport's default is SSE-framed POST responses, which Cloudflare Tunnel
        # buffers until the stream closes (`docs/00-research.md` §5.1). Single JSON
        # responses are the entire reason the tunnel path works. Do not remove.
        json_response=True,
        custom_starlette_routes=[Route("/healthz", _healthz, methods=["GET"])],
        # Explicit: with host 0.0.0.0 the SDK does not auto-enable DNS-rebinding
        # protection. Behind Caddy the Host header is the public hostname, and the
        # tunnel is the trust boundary.
        host=cfg.host,
    )

    return BearerAuthMiddleware(app, StaticTokenVerifier(cfg.token))
