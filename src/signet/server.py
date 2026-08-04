"""ASGI wiring: the MCP surface at /mcp, an unauthenticated /healthz, bearer auth in front."""

from __future__ import annotations

import json
import logging

import mcp.types as types
from mcp.server.lowlevel import Server
from starlette.requests import Request as HTTPRequest
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config as config_module
from . import coreschema, db
from .auth import BearerAuthMiddleware, DbTokenVerifier, Principal
from .config import Config
from .envelope import Request
from .llm import LLM
from .registry import Registry
from .router import Router
from .verbs import INSTRUCTIONS, PROMPT_DESCRIPTIONS, PROMPT_TEXT, TOOLS, Verbs
from .web import app as web_app

logger = logging.getLogger("signet")

PROMPTS = [types.Prompt(name=name, description=PROMPT_DESCRIPTIONS[name]) for name in PROMPT_TEXT]


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

    async def on_list_prompts(ctx: object, params: object) -> types.ListPromptsResult:
        """The Pebble app calls prompts/list during setup and refuses to connect to a server
        that does not advertise the capability, so this is not optional for the ring.

        Every prompt here takes no arguments, because the app filters to
        `arguments == null` and would silently hide anything else. The user ticks which ones
        they want, and the text is concatenated into the on-device model's context, so this is
        a second free channel alongside `instructions`.
        """
        return types.ListPromptsResult(prompts=PROMPTS)

    async def on_get_prompt(
        ctx: object, params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        text = PROMPT_TEXT.get(params.name)
        if text is None:
            return types.GetPromptResult(
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text="Unknown prompt."),
                    )
                ]
            )
        return types.GetPromptResult(
            description=PROMPT_DESCRIPTIONS.get(params.name),
            messages=[
                types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))
            ],
        )

    async def on_list_resources(ctx: object, params: object) -> types.ListResourcesResult:
        """Advertised and empty. signet exposes no resources, but a client that probes for
        them should get an empty list rather than a capability error."""
        return types.ListResourcesResult(resources=[])

    return Server(
        "signet",
        version="0.2.0",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
        on_list_resources=on_list_resources,
    )


def _principal_from(ctx: object) -> Principal | None:
    """The auth middleware stashes the resolved caller on the ASGI scope. The SDK does not
    hand that through, so it is read back off the request context when available."""
    request = getattr(ctx, "request", None)
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        return scope.get("signet.principal")
    return None


class RefuseNotificationStream:
    """Answer `GET /mcp` with 405 instead of opening a stream.

    Streamable HTTP lets a client open a long-lived GET for server-initiated messages. The SDK
    serves it as SSE, and `json_response=True` does not cover it: that flag only governs POST
    responses. Cloudflare Tunnel buffers SSE until the stream closes, so the Pebble app
    completed its handshake, opened this GET, and hung until its socket timed out.

    The stream is optional. The spec says a server that does not offer it returns 405 and the
    client carries on without it, which is right here because signet never pushes anything: an
    answer is the response to the call that asked for it.
    """

    def __init__(self, app: ASGIApp, path: str = "/mcp") -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and scope["path"].rstrip("/") == self.path
        ):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": "This server does not offer a notification stream.",
                    },
                    "id": None,
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"allow", b"POST, DELETE"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


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

    portal = web_app.build(cfg)
    routes = [Route("/healthz", _healthz, methods=["GET"])]
    if portal is not None:
        routes.append(Mount("/app", app=portal))
        logger.info("admin portal mounted at /app")
    else:
        logger.warning("no SIGNET_ADMIN_PASSWORD set, admin portal is disabled")

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        # The transport's default is SSE-framed POST responses, which Cloudflare Tunnel
        # buffers until the stream closes (docs/00-research.md section 5.1). Single JSON
        # responses are the entire reason the tunnel path works. Do not remove.
        json_response=True,
        custom_starlette_routes=routes,
        host=cfg.host,
    )

    # Outermost, so the stream is refused before auth or the transport sees it.
    return RefuseNotificationStream(BearerAuthMiddleware(app, DbTokenVerifier(cfg)))
