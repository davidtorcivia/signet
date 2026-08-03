# signet

A self-hosted MCP server for the [Pebble Index 01](https://repebble.com) ring.

Press the ring, talk. The phone transcribes it and picks a tool. When it picks one of signet's,
the request goes to your own server instead of someone else's cloud.

A signet is a ring you press to authorize things.

## Status

Early. One tool works end to end: `capture` saves whatever you say to a journal file. It runs in
Docker behind a Cloudflare Tunnel and has been verified against the real protocol the Pebble app
speaks.

Still to build: the router, the other three verbs (`ask`, `schedule`, `do`), calendar, and the
admin portal. The plan is in [docs/03-implementation-plan.md](docs/03-implementation-plan.md).

## What we learned about the Pebble app

This is probably the useful part if you own an Index 01. The app lets you add your own MCP
server, but there are no docs, and a few things will bite you. All of this was read out of
[coredevices/mobileapp](https://github.com/coredevices/mobileapp) and then verified against a
running server.

**The app speaks MCP 2025-06-18.** It ships MCP Kotlin SDK 0.8.3, which tops out there. Current
SDKs default to 2026-07-28, where the whole initialize handshake no longer exists. Both eras can
be served from one endpoint. The Python SDK still supports the old handshake, so no shim is
needed.

**Return all your tools in one page.** The client hits a literal `TODO("Handle pagination")` and
throws if you send a `nextCursor`.

**Serve single JSON responses, not SSE.** Cloudflare Tunnel buffers SSE until the stream closes,
so an SSE response is one the ring never receives. The Python SDK's Streamable HTTP transport
frames POST responses as SSE by default, which passes every local test and then hangs behind the
tunnel. Turn it off with `json_response=True`.

**Auth is one header, used verbatim.** No OAuth, no dynamic registration. You type a single
`Authorization` value into the app, so it has to include the word `Bearer`. Cloudflare Access
service tokens will not work, because they need two headers the app cannot send.

**`instructions` from `initialize` gets injected into the on-device model's context.** Free
system prompt channel. Use it.

**Results can render as native items in the app feed.** Set `_meta.coreSchema` to the integer 1
and return `structuredContent` with both `output` and `semanticResult`. Both keys are required:
the app reads them with `getValue()` and throws if either is missing. The discriminator is
`type`, and unknown fields are rejected rather than ignored, so send exactly the fields the
variant declares.

**You get no session context.** No user id, no device id, no history. Every call arrives cold,
so the server has to keep its own state.

## Run it

```bash
uv sync
export SIGNET_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
uv run python -m signet
```

Then check it, including the parts the ring cares about:

```bash
uv run pytest
uv run python scripts/legacy_handshake.py http://127.0.0.1:8300 "$SIGNET_TOKEN"
```

`legacy_handshake.py` is stdlib only, so you can run it against a deployed server from any box
with Python.

To point the phone at it, add an MCP server in the Pebble app with your URL ending in `/mcp`,
streamable set to true, and the auth header `Bearer <your token>`.

## Deploy

Docker Compose, one container, no database yet. See [DEPLOY.md](DEPLOY.md).

## Docs

| File | What's in it |
| --- | --- |
| [docs/00-research.md](docs/00-research.md) | How the ring works and the exact MCP dialect its app speaks |
| [docs/01-design-options.md](docs/01-design-options.md) | Four architectures considered, and why one won |
| [docs/02-architecture.md](docs/02-architecture.md) | The design: inlets, router, capabilities, outlets |
| [docs/03-implementation-plan.md](docs/03-implementation-plan.md) | Build order, with what has been verified so far |

## License

MIT
