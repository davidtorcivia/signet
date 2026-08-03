# Decisions and architecture

Supersedes the open questions at the end of [`01-design-options.md`](01-design-options.md).

---

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| 1 | Where does the thinking happen? | **Option C.** Quick tool recognition on the phone; hard cases route to a server-side model. |
| 2 | Async or hard-bounded? | **Async is fine for hard tasks.** Fast paths stay synchronous; `do()` may defer and push. |
| 3 | Memory owner | **signet owns it.** The feed is the corpus. |
| 4 | Todo storage | **Deferred — explicit non-goal for v1.** The Pebble app already routes todos to its own store / Apple Reminders / Google Tasks; signet won't compete with that until there's a reason to. Captures land in signet's journal, which is searchable. Revisit if the split becomes annoying. |
| 5 | Reach into the existing stack | **Hooks yes, but capability-scoped and approval-gated.** See [Security](#security-model). |
| 6 | Model / cost | **`deepseek/deepseek-v4-flash-0731`** — $0.09/M in, $0.18/M out, 1M context, supports `tools`, `structured_outputs`, `reasoning_effort`. Cost is a rounding error (see below). |
| 7 | Repo | **Private GitHub for now.** The research doc is publishable later on its own. |

Plus the standing constraint from this round: **build it as a routing station.** Every part that
could plausibly be swapped or added to later gets a seam now.

### What the model price actually buys

A `do()` call with generous context — recent journal, next two weeks of calendar, contacts,
tool schemas — is maybe 6k in / 600 out. That's **$0.00065**. Fifty a day is **~$1/month.**

This is worth dwelling on because it inverts a normal design instinct. With a 1M-context model at
$0.09/M, **retrieval is more expensive to build than it is to skip.** Don't build embeddings, don't
build a vector store, don't build clever context selection. Stuff the whole relevant corpus into the
prompt and let the model sort it out. Full-text search over the journal is there for when the corpus
outgrows the window, which at your volume is years away.

Cost caps still go in — not for economics, but as a runaway-loop circuit breaker.

---

## The shape: inlets → router → capabilities → outlets

The extensibility ask is the load-bearing requirement now, so the architecture is four pluggable
layers with a normalized envelope between each. Everything else follows from that.

```
  INLETS                ROUTER               CAPABILITIES            OUTLETS
  ────────              ──────               ────────────            ───────
  MCP tools    ─┐                        ┌─ journal.*            ┌─ MCP result
  (the ring)    │   ┌──────────────┐     │  calendar.*           │  (semanticResult)
  webhook       ├──>│  Request     │────>├─ search.*         ────┤  ntfy push
  HTTP API      │   │  envelope    │     │  home.*               │  web feed
  cron          │   │  + router    │     │  media.*              │  webhook out
  web app       ┘   └──────────────┘     └─ mcp_upstream.*       └─ TTS / watch
                           │                      ▲
                    deterministic rules            │
                    first, then DeepSeek       registry:
                    classification             drop-in modules
```

**Request envelope** — every inlet normalizes to the same thing, so a capability never knows or
cares whether it was triggered by the ring, a cron entry, or a curl:

```python
Request(
  id, received_at,
  text,                 # transcript or command
  source,               # "mcp:ring" | "webhook:index" | "cron" | "web" | "api"
  client,               # resolved from bearer token -> scopes, limits, tool visibility
  audio?, hints?,       # raw audio when the webhook path is used
  reply_to,             # which outlet(s) get the result
)
```

**Router** — deterministic rules run first (prefix matches, obvious verbs, "remember that…"), then
a single DeepSeek call classifies whatever's left. Rules are data, not code, so they're editable in
the web app. The router's output is a `Plan`: one capability + arguments, or "run the agent loop."

**Capabilities** — the pluggable unit and the whole point of the design. A capability is a directory
with a manifest and a handler:

```python
# capabilities/calendar/manifest.py
Capability(
    name="calendar.create_event",
    description="Create an event on a Google Calendar.",
    schema=CreateEventArgs,           # pydantic -> JSON Schema
    scopes=["calendar:write"],
    exposure="internal",              # "internal" | "mcp" | "both"
    tier="fast",                      # "instant" | "fast" | "slow"  -> sync vs async
    destructive=False,                # True -> approval queue
    semantic=lambda r: CalendarEventCreation(...),   # -> Pebble feed rendering
)
```

- `exposure` is what keeps the ring's tool list at four verbs while the internal toolbelt grows
  without limit. Only `mcp`/`both` capabilities appear in `tools/list`, filtered further by the
  calling token's scopes.
- `tier` decides sync vs. async automatically, so no capability author has to think about the
  Cloudflare timeout.
- `semantic` is where the `coreSchema` mapping lives, so native Pebble rendering is one line per
  capability rather than a special case.

**`mcp_upstream` is just another capability provider.** Point it at any remote MCP server and its
tools mount into the registry as internal capabilities, namespaced. That's how signet borrows
instead of building — Home Assistant's MCP integration, mem0, anything that shows up later — and
it's the "routing station" property made concrete. Option D from the design doc, demoted to a
plugin, which is where it belonged.

**Outlets** — the same result can go to several places. An async `do()` returns "on it" through the
MCP outlet immediately and the real answer through ntfy when it lands.

### Adding a capability, end to end

Drop a directory in `capabilities/`, declare the manifest, write the handler. It is now: callable by
the internal agent, routable by rules, visible in the web app, permission-checked, logged, and (if
`exposure` says so) an MCP tool. No wiring anywhere else. **That's the test for whether this design
is doing its job** — if adding a capability ever requires touching the router, the registry, or the
MCP layer, the seam is in the wrong place.

---

## Security model

A voice agent triggered by a button you can press through your trouser pocket, holding a token that
can reach Home Assistant, deserves more than "it's on my LAN."

**Scopes.** Each token carries a scope set; each capability declares what it needs. `journal:write`
is not `home:control` is not `media:control`. The ring's token gets the minimum that makes it
useful. A capability with an unmet scope is invisible in `tools/list` and rejected if called anyway.

**Approval queue for destructive things.** The ring cannot answer a confirmation prompt — no session
context, no elicitation support in that client. So anything marked `destructive` doesn't execute; it
**queues**, fires an ntfy notification with an approve/deny link, and runs on one tap. Unlocking
doors, deleting things, spending money, anything in transmission. This is the pattern that makes
"hooks into everything" safe rather than alarming, and it costs one notification.

**Blast radius.** Its own Docker network. Outbound allowlist rather than open egress. No
`docker.sock` — the existing webhook-server has it and signet must not copy that. Read-only mounts.
Per-token rate limits and a global daily action cap.

**Auditability.** Every request, plan, capability call, argument, and result is in the feed, with a
kill switch that disables all non-`journal` capabilities in one click.

**Prompt injection is a live threat here**, because `search.*` and any future email/message inlet
pull untrusted text into a context that can call tools. Mitigation: untrusted content is fenced and
labeled in the prompt, and capabilities above `journal:write` are never callable in a turn whose
context includes untrusted fetched text without going through the approval queue.

---

## Build phases

> Expanded into executable, verified tasks in [`03-implementation-plan.md`](03-implementation-plan.md) —
> implement from there; this section is the summary.

**P0 — the spike (blocks everything).** One `capture` tool, a bearer token, behind the tunnel.
Connect the real Pebble app. Confirm: the MCP settings screen is reachable on a shipping build, a
modern server can still serve its 2025-06-18 handshake, Streamable HTTP survives the tunnel, and
`_meta.coreSchema` renders. Four unknowns, one afternoon, and any of them failing changes the design.

**P1 — the spine.** Registry, envelope, router, tokens/scopes, the four verbs (`capture`, `ask`,
`schedule`, `do`), journal storage with FTS, and the web app feed. At the end of P1 the ring is
genuinely useful for capture and lookup.

**P2 — calendar and async.** Google OAuth consent in the web app, the calendar capabilities, the job
queue, ntfy outlet, corrections UI.

**P3 — the routing station.** `mcp_upstream` provider, Home Assistant hooks behind the approval
queue, cron briefings, rules editor.

Everything past P3 should be a capability, not a change to signet.

---

## Deliberate non-goals for v1

- A todo/task store (decision 4).
- Embeddings or a vector database (the context window is cheaper).
- Any second MCP client as a design driver.
- Streaming responses (the tunnel buffers them; results are single JSON).
- Multi-user. One user, several tokens.
