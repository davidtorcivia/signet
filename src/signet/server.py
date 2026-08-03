"""ASGI wiring: the MCP surface at /mcp, an unauthenticated /healthz, bearer auth in front."""

from __future__ import annotations

import logging

import mcp.types as types
from mcp.server.lowlevel import Server
from starlette.requests import Request as HTTPRequest
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config as config_module
from . import coreschema, db
from .auth import BearerAuthMiddleware, DbTokenVerifier, Principal
from .config import Config
from .envelope import Request
from .llm import LLM
from .registry import Registry
from .router import Router
from .verbs import INSTRUCTIONS, TOOLS, Verbs

logger = logging.getLogger("signet")


def build_mcp_server(cfg: Config, verbs: Verbs) -> Server:
    async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        # No nextCursor, ever. The Pebble client hits `TODO("Handle pagination")` and throws
        # if one is present (docs/00-research.md section 2). One page, four verbs.
        return types.ListToolsResult(tools=TOOLS)

    async def on_call_tool(
        ctx: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        principal = _principal_from(ctx) or Principal(client_id="unknown")

        from .verbs import ARG_NAMES

        text = str(arguments.get(ARG_NAMES.get(params.name, "text"), "") or "").strip()
        if not text:
            return coreschema.result(
                "Nothing to do, no text was provided.",
                coreschema.generic_failure("Nothing to do.", llm_recoverable=True),
                is_error=True,
            )

        request = Request(text=text, source="mcp:ring", client=principal, verb=params.name)
        conn = db.connect(cfg.db_path)
        try:
            outcome = await verbs.call(conn, params.name, request)
        finally:
            conn.close()

        return coreschema.result(outcome.output, outcome.semantic, is_error=outcome.is_error)

    return Server(
        "signet",
        version="0.2.0",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _principal_from(ctx: object) -> Principal | None:
    """The auth middleware stashes the resolved caller on the ASGI scope. The SDK does not
    hand that through, so it is read back off the request context when available."""
    request = getattr(ctx, "request", None)
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        return scope.get("signet.principal")
    return None


async def _healthz(request: HTTPRequest) -> JSONResponse:
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


def build_verbs(cfg: Config) -> Verbs:
    registry = Registry()
    registry.discover()
    llm = LLM(cfg.openrouter_api_key, model=cfg.model)
    router = Router(llm, rules_path=cfg.data_dir / "rules.yaml")
    return Verbs(cfg=cfg, registry=registry, llm=llm, router=router)


def create_app(cfg: Config | None = None):
    cfg = cfg or config_module.load()
    config_module.set_cached(cfg)
    init_storage(cfg)

    server = build_mcp_server(cfg, build_verbs(cfg))

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        # The transport's default is SSE-framed POST responses, which Cloudflare Tunnel
        # buffers until the stream closes (docs/00-research.md section 5.1). Single JSON
        # responses are the entire reason the tunnel path works. Do not remove.
        json_response=True,
        custom_starlette_routes=[Route("/healthz", _healthz, methods=["GET"])],
        host=cfg.host,
    )

    return BearerAuthMiddleware(app, DbTokenVerifier(cfg))
