# Research: Pebble Index 01, its MCP dialect, and the erebus target

Gathered 2026-08-03. Everything below is either read out of the Pebble app source
(`coredevices/mobileapp`, branch `master`), read off erebus directly, or cited to a URL.

---

## 1. What the Index 01 is

A $75 non-rechargeable smart ring: **a button, a microphone, ~a bit of flash, and a BLE chip.**
No sensors, no screen, no health tracking. Press-and-hold to record, release to stop; the clip
syncs to the phone. Silver-oxide cell, ~12–15h of cumulative recording, quoted as ~2 years at
10–20 short notes/day, then you mail it back for recycling. Announced 2025-12-09, mass production
early 2026, $99 post-presale.
([TechRadar](https://www.techradar.com/health-fitness/pebble-is-reinventing-voice-assistants-and-smart-rings-in-one-device-meet-the-pebble-index-01),
[Hackster](https://www.hackster.io/news/core-devices-unveils-the-pebble-index-01-a-disposable-smart-ring-microphone-c103c26e4f24))

### The pipeline (this is the part that matters)

```
ring button ──BLE──> Pebble mobile app ──> transcription ──> agent loop ──> MCP tool call
                                              │                  │
                                    Parakeet 0.6B v3        Needle (Cactus Compute),
                                    on-device, ~700MB       tiny on-device tool-calling
                                    (or cloud option)       model — or cloud option
```

Results are sorted into four buckets in the app's feed: **Todos, Notes, Answers, Actions.**

Gestures, per Migicovsky's own writeup
([repebble.com/blog/how-i-use-my-index-01-production-update](https://repebble.com/blog/how-i-use-my-index-01-production-update)):

| Gesture | Route |
| --- | --- |
| Single click-hold + speak | Memory path — triaged into todo / note / action |
| Double click-hold + speak | General-purpose agent path — "Claude Sonnet + web search", answer shown on watch or as a notification |
| Triple click | Next track (Android only) |

Three customization surfaces, in increasing depth:

1. **Button actions** — bind single/double click to Tasker, Home Assistant, camera, etc.
2. **Webhooks** — fire every recording (raw audio, transcript, or both) at your own server.
3. **MCP** — every built-in action *is* an MCP tool; you can add your own servers and swap out
   the defaults.

The app is GPL-3.0/commercial dual-licensed, Kotlin Multiplatform + Compose, and takes PRs:
[github.com/coredevices/mobileapp](https://github.com/coredevices/mobileapp).

**Consequence for us:** signet has *two* possible integration points — the MCP surface (structured,
the agent decides when to call us) and the webhook surface (dumb, fires on everything). They are
not mutually exclusive and Option D in the design doc uses both.

---

## 2. The exact MCP dialect the Pebble app speaks

This is the highest-value part of the research. Read out of source, not docs — there are no docs.

### Client SDK and protocol version

`gradle/libs.versions.toml`: `mcp = "0.8.3"` → **MCP Kotlin SDK 0.8.3**.

That version's `kotlin-sdk-core/.../types/common.kt`:

```kotlin
public const val LATEST_PROTOCOL_VERSION: String = "2025-06-18"
public const val DEFAULT_NEGOTIATED_PROTOCOL_VERSION: String = "2025-03-26"
public val SUPPORTED_PROTOCOL_VERSIONS: List<String> = listOf(
    LATEST_PROTOCOL_VERSION, "2025-03-26", "2024-11-05",
)
```

> **The app is a handshake-era MCP client. It tops out at 2025-06-18.**

Meanwhile the current spec is **2026-07-28**, which is a much larger break than usual: sessions and
the `Mcp-Session-Id` header are gone, the `initialize`/`notifications/initialized` handshake is
*removed* in favour of `server/discover`, `Mcp-Method` and `Mcp-Name` headers are now required on
Streamable HTTP requests, list results carry `ttlMs`/`cacheScope`, and **HTTP+SSE is formally
deprecated**. ([spec changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
[MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/))

So signet must serve **handshake-era clients** (Pebble) *and* modern ones (hermes, Claude Code).
Python `mcp` 2.x carries a `_streamable_http_modern.py` alongside the legacy path and its client
has a `mode="auto"|"legacy"|<pinned version>` switch, so both eras coexist — but **this is the #1
thing to verify with a real handshake before writing any features.**
([python-sdk/docs/protocol-versions.md](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md))

### Transport and auth — `mcp/src/commonMain/kotlin/coredevices/mcp/client/HttpMcpIntegration.kt`

```kotlin
enum class HttpMcpProtocol { Streaming, Sse }

class HttpMcpIntegration(
    override val name: String,
    implementation: Implementation,
    private val url: String,
    protocol: HttpMcpProtocol = HttpMcpProtocol.Sse,   // <-- SSE is the DEFAULT
    authHeader: String? = null
)
```

and the transport gets exactly one thing:

```kotlin
if (authHeader != null) {
    defaultRequest { header("Authorization", authHeader) }
}
```

The stored config (`index-ai/.../entity/mcp_sandbox/HttpMcpServerEntity.kt`) is the whole surface
area a user can configure:

```kotlin
@Entity data class HttpMcpServerEntity(
    val id: Long, val cachedTitle: String, val name: String,
    val url: String,
    val streamable: Boolean,      // false = legacy HTTP+SSE, true = Streamable HTTP
    val authHeader: String?,      // one raw header value, verbatim
    val includedPrompts: List<String>
)
```

**Implications, all load-bearing:**

- **No OAuth. No DCR. No `CF-Access-Client-Id`.** One static `Authorization` value, typed by hand
  into the app. So auth is a long random bearer token, and **Cloudflare Access service tokens are
  off the table for the Pebble path** — they need two custom headers the app cannot send.
- `authHeader` is used verbatim, so it must include the scheme: `Bearer <token>`.
- Default transport is legacy SSE. We should ship Streamable HTTP and tell the app to use it
  (see the Cloudflare buffering constraint in §5).
- Add/edit UI lives in `experimental/.../ui/screens/settings/mcp/McpServers.kt` — **the `experimental`
  module.** Worth confirming on the actual shipping build that this screen is reachable (dev flag?
  hidden menu?) before betting the design on it.

### What the client does with our server

Also from `HttpMcpIntegration.kt`:

- **`tools/list` pagination is not implemented** — literally `if (result.nextCursor != null) TODO("Handle pagination")`.
  Return every tool in one page or the app throws.
- Tool list is cached for **30 seconds**.
- **`instructions` from `initialize` is injected into the model's context** (`getExtraContext()`
  returns `client.serverInstructions`). This is a free system-prompt channel — use it.
- **Prompts are supported, but only prompts with no arguments** (`filter { it.arguments == null }`),
  and only ones the user has ticked in `includedPrompts`. Their text is concatenated into context.
  Second free prompt channel, user-gated.
- **`SessionContext` is explicitly NOT forwarded to remote servers** — the comment says so. We get
  no user id, no device id, no conversation history. Every call is context-free; signet has to keep
  its own state.

### The `coreSchema` result contract — how to render natively

Ordinary results work fine (text content → string to the LLM). But there's a private extension that
makes results show up as first-class items in the app's feed:

```kotlin
val isCoreSchema = result.meta?.containsKey("coreSchema") == true
// schemaVersion <= 1:
//   structuredContent["semanticResult"]  -> deserialized into SemanticResult
//   structuredContent["output"]          -> the string handed to the LLM
```

So a tool result should carry `_meta: {"coreSchema": 1}` and
`structuredContent: {"output": "...", "semanticResult": {...}}`.

`SemanticResult` variants (from `mcp/src/commonMain/kotlin/coredevices/mcp/data/ToolCallResult.kt`,
`@SerialName` is the discriminator):

| Variant | Fields |
| --- | --- |
| `TaskCreation` | `title`, `deadline`, `localReminderId?`, `notifyBeforeMillis?` |
| `ListItemCreation` | `content`, `listUsed?`, `remindAt?`, `resolvedListId?` |
| `AlarmCreation` | `fireTime` (LocalTime) |
| `CalendarEventCreation` | `title`, `startTime`, `endTime`, `location?` |
| `TimerCreation` | `requestedDuration?`, `fireTime` |
| `SupportingData` | `summary?`, `assistiveOnly`, `question?` |
| `Response` | `text`, `question?` — *the agent's spoken answer; surfaced in the completion notification* |
| `MessageSent` | `recipientName`, `text`, `contactId` |
| `ActionLogged` | `toolName`, `title`, `success`, `body` |
| `GenericSuccess` / `GenericFailure` | `GenericFailure(userErrorMessage?, llmRecoverable, forceFallbackTool)` |

`Response` and `SupportingData` are the two signet will live in: `Response` for "here's your answer,
show it on the watch", `SupportingData` for "here's data, keep thinking".

`GenericFailure.llmRecoverable` is worth using properly — it tells the on-phone model whether
retrying is pointless.

### Built-ins we'd be competing with / replacing

`experimental/.../agent/builtin_servlets/`: `js`, `notes`, `clock`, `calendar`, `reminders`,
`messaging`. There is already a device-level **CalendarServlet** — on Android the device calendar
provider is usually the Google account, so "put it on my calendar" may already half-work locally.
signet's calendar value is server-side reach (multiple calendars, invitees, free/busy, scheduling
logic), not basic event creation.

Servers are assigned to **sandbox groups** (`McpSandboxGroupEntity`, `SandboxModelType`) which gate
which model sees which tools — so signet's tools can be scoped to the double-click "big agent" path
without polluting the fast memory path.

---

## 3. Other MCP clients — keep the door open, don't build for them

**The ring is the customer.** signet is designed for the Index 01. But being a plain, standards-
compliant remote MCP server costs nothing extra, and it means any other client can be pointed at it
later with no rework. Treat the list below as a *don't-preclude-this* checklist, not requirements.

The cheapest second consumer to sanity-check against is one you already run: **Claude Code**, which
connects to remote MCP servers over Streamable HTTP with a bearer token. If it can list and call
signet's tools, the server is standards-clean.

[hermes-agent](https://github.com/NousResearch/hermes-agent) (Python, MIT) is the eventual candidate.
**It is not installed on erebus** — the only "hermes" hits on that box are `Hermes-3-Llama-3.1-8B`
GGUFs under a local models directory. Adding it later is its own container and its
own project. What it would want, from `tools/mcp_tool.py` and `hermes_cli/mcp_config.py`:

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  signet:
    url: "https://signet.example.com/mcp"
    headers:
      Authorization: "Bearer sk-..."      # or ${ENV_VAR} refs, resolved from ~/.hermes/.env
  # transport: sse                        # optional; default is Streamable HTTP
```

Only two of its behaviours imply anything about our design, and both are things we'd want anyway:

- It **probes the URL** and raises `NonMcpEndpointError` if the endpoint answers `text/html` instead
  of `application/json`/`text/event-stream`. → Keep `/mcp` off any hostname that serves the web app's
  HTML. Good hygiene regardless.
- Its default transport is **Streamable HTTP** (Pebble's default is SSE). → We're serving Streamable
  HTTP anyway because of the Cloudflare buffering constraint in §5.

Note that hermes ships its own memory (FTS5), skills, and cron. If it ever does land here, that's a
scope collision to resolve *then* — it is not a reason to leave gaps in signet now.

---

## 4. The target server

signet runs on a home Linux box (referred to as `erebus` throughout these docs) with Docker and
an existing Cloudflare Tunnel. Host specifics are kept out of this repo; the parts that actually
shape the design are:

- **cloudflared already runs on the host** in `--token` mode. That matters because ingress is
  then managed remotely in the Zero Trust dashboard, and the local
  `~/.config/cloudflared/config.yml` is ignored. Adding a hostname is a dashboard change, not a
  file edit.
- **A reverse proxy (Caddy) fronts most services**, but signet's `/mcp` deliberately skips it.
  cloudflared is on the host, so the tunnel points straight at `localhost:8300`. The P1 web app
  is the opposite case: it goes through the proxy so it can sit behind forward auth.
- **An ntfy instance is already exposed through the tunnel**, which is the obvious push channel
  for async job completion and "I did the thing" receipts.
- **Home Assistant, a local Whisper (GPU) and a local TTS** are all already running, so the
  tier 2 and tier 3 features in `01-design-options.md` are integrations rather than new
  infrastructure.
- **An MCP server has been deployed on this box before**, so the compose and proxy patterns
  already exist and signet should copy them rather than invent one.
- **Convention:** one directory per app under `/srv/apps/<name>/` with a `docker-compose.yml`.

If you are reading this as a stranger: none of the above is required. signet needs Docker, a way
to reach it from the internet, and a bearer token.

---

## 5. Hard constraints (the ones that will bite)

1. **Cloudflare buffers SSE, especially GET streams.** Long-standing, unresolved:
   [cloudflared#1449](https://github.com/cloudflare/cloudflared/issues/1449),
   [#1095](https://github.com/cloudflare/cloudflared/issues/1095),
   [#199](https://github.com/cloudflare/cloudflared/issues/199). Events land only when the stream
   closes. Cloudflare's own guidance is to stop using HTTP+SSE and serve
   [Streamable HTTP](https://blog.cloudflare.com/streamable-http-mcp-servers-python/).
   → **Serve Streamable HTTP, return single non-streamed JSON responses, set the app's
   `streamable` flag to true.** Keep an SSE endpoint only as a fallback for testing on the LAN.

2. **The ~100 s edge timeout.** A proxied request that takes longer than the origin timeout gets a
   524. A multi-step OpenRouter agent loop with web search will blow through that on its bad days.
   → Either keep the synchronous path budgeted (hard deadline ~60–75 s, cheap fast model, capped
   tool-loop iterations) or make long work **async**: return "working on it" immediately, finish in
   the background, deliver via ntfy / a webhook back into the Pebble app / the web app feed.
   This is the single biggest architectural fork — see Option C.

3. **Protocol-era split.** Pebble is stuck in the 2025-06-18 handshake era while every current SDK
   has moved to 2026-07-28. Whatever server framework we pick has to still speak the old handshake —
   verify that with a real connection *before* writing features, not after. Serving the modern era
   too is close to free and is what keeps other clients possible later.

4. **No pagination, one page of tools.** Also: too many tools degrades a tiny on-device model. Keep
   the tool count small (≤ ~8) on principle. If a big-model client ever needs the full set, per-token
   filtering can widen it then — but don't design around a consumer that doesn't exist.

5. **No session context from Pebble.** No user identity, no thread. Any continuity ("what did I say
   about that yesterday") must come from signet's own store, keyed on the bearer token.

6. **Static bearer token in a phone app.** It cannot be rotated by the app, so: long random token,
   one token per client, revocable in the web app, rate-limited, and the tunnel hostname should not
   be guessable. Consider Cloudflare WAF rules on that hostname since Access is unavailable.

7. **Google Calendar auth.** Personal Google accounts can't use service-account impersonation, so
   this is an OAuth installed-app flow: consent once in the web app, store the refresh token
   encrypted, refresh server-side. That consent screen is a good reason for the web app to exist.

---

## 6. Sources

- [repebble.com/blog/how-i-use-my-index-01-production-update](https://repebble.com/blog/how-i-use-my-index-01-production-update)
- [repebble.com/blog/meet-pebble-index-01-external-memory-for-your-brain](https://repebble.com/blog/meet-pebble-index-01-external-memory-for-your-brain)
- [github.com/coredevices/mobileapp](https://github.com/coredevices/mobileapp) — `mcp/`, `index-ai/`, `experimental/` modules
- [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — `tools/mcp_tool.py`, `hermes_cli/mcp_config.py`
- [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog) · [announcement](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [python-sdk protocol-versions doc](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md)
- [Cloudflare: streamable HTTP MCP servers](https://blog.cloudflare.com/streamable-http-mcp-servers-python/) · [cloudflared#1449](https://github.com/cloudflare/cloudflared/issues/1449)
- [OpenRouter tool-calling models](https://openrouter.ai/collections/tool-calling-models)
- [TechRadar Index 01](https://www.techradar.com/health-fitness/pebble-is-reinventing-voice-assistants-and-smart-rings-in-one-device-meet-the-pebble-index-01) · [Hackster](https://www.hackster.io/news/core-devices-unveils-the-pebble-index-01-a-disposable-smart-ring-microphone-c103c26e4f24)
