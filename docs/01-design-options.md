# Design options

Read [`00-research.md`](00-research.md) first — the constraints in §5 drive most of what follows.

The shape of the problem: a **tiny model on your phone** decides whether to call signet at all, and
if so, with what arguments. Whatever signet exposes has to be legible to that small model and has to
answer inside a Cloudflare request window. That's the whole design brief.

**The ring is the only customer.** Other MCP clients — hermes eventually, Claude Code today — are a
door we leave unlocked, not a design driver. Being a plain standards-compliant server costs nothing;
shaping the tool surface around a hypothetical big-model consumer costs a lot. So: no compromises
for absent clients.

---

## Option A — One brain, one door

signet exposes a **single tool**, roughly:

```
assistant(request: string, context?: string) -> Response
```

The on-phone model's only job is "is this for signet? then pass the words through." Everything
else happens server-side: signet runs its own agent loop on OpenRouter with the real toolbelt
(calendar, search, home, notes) and returns a `Response` semantic result that the app speaks/shows.

**Good:** dead simple for the tiny model — one tool, one string argument, nothing to get wrong.
Model choice, prompts, and tools are all server-side, so you iterate without touching the phone.
Naturally matches the double-click "general agent" gesture.

**Bad:** every request pays full agent-loop latency, so the 100 s ceiling is always in play — even
for "add milk to the list," which should be instant. The phone's model can't compose signet with its
local tools because it can't see inside. And debugging is "the black box did something."

---

## Option B — Flat toolbelt

signet exposes ~15 discrete, boring tools: `calendar_create_event`, `calendar_find_time`,
`calendar_list_today`, `web_search`, `web_read`, `home_run_scene`, `note_append`, `memory_search`,
`notify`, … No server-side LLM at all; OpenRouter only shows up for small jobs like NL date parsing.

**Good:** fast, cheap, debuggable, deterministic. Failures are legible and each tool is
independently testable. No OpenRouter bill for routine captures.

**Bad:** it hands the hardest job — multi-step planning — to a ~1B on-device model that will fumble
it. Blows the tool budget (§5.4). "Move my 3pm to tomorrow and tell Sarah" needs four coordinated
calls; Needle isn't going to make them. This is the option that looks tidy and fails in daily use.

---

## Option C — Hybrid: fast lane + escalation *(recommended)*

Three or four **cheap deterministic tools** for the things the small model can get right on its own,
plus **one escalation tool** that runs the full server-side agent loop, plus an **async escape
hatch** for anything slow.

```
  capture(text, kind?)          → write to the journal/inbox; always succeeds, <200ms, no LLM
  ask(question)                 → fast lookup: one model call + search, hard 45s budget
  schedule(request)             → calendar-specific NL → event, returns CalendarEventCreation
  do(request, deadline_hint?)   → full agent loop; if it'll be slow, returns immediately
                                    ("On it — I'll ping you") and pushes the answer via ntfy
```

Four verbs. A small model can pick between them; `do` is the catch-all when it can't.

The **async escape hatch** is what makes the 100 s ceiling a non-issue. `do()` starts a job, returns
a `SupportingData`/`Response` straight away, and delivers the real result out-of-band — ntfy push,
the web app feed, or (nice touch) a webhook back into the Pebble app so the answer lands on your
watch.

Internally the tools are thin wrappers over a **private** toolbelt (calendar, search, home, notes)
that only signet's own server-side agent sees. That toolbelt is where the work actually goes, and
keeping it private is what lets it be as fine-grained and numerous as it needs to be without ever
touching the phone's tool budget. If a big-model client ever shows up, exposing part of it publicly
is a `tools/list` filter keyed on the bearer token — an afternoon, not a redesign.

**Good:** every request served on the path that fits it. Captures are instant and free; slow work
doesn't get truncated by an edge timeout; the tool surface stays legible to a 1B model.

**Bad:** most moving parts. Two code paths to keep honest, a job queue, and a push channel.

---

## Option D — Gateway / aggregator

signet is primarily a **multiplexer**: it proxies and merges *other* MCP servers (mem0, Home
Assistant, a Google Calendar server, whatever) behind one URL, one token, one tool namespace — with
filtering, renaming, logging, and rate limits in the middle. Its own agent loop is just one more
upstream.

**Good:** the Pebble app can only hold a handful of hand-typed servers with hand-typed tokens; a
gateway collapses that to one. Credentials stay on erebus. Adding a capability becomes "add an
upstream," not "write a tool." Also solves the no-OAuth problem generally: signet does OAuth
upstream, clients present a static bearer.

**Bad:** on its own it's plumbing, not intelligence — it does nothing about the small-model planning
problem. Upstream tool names and descriptions are written for big models. Tool-count explosion
fights §5.4 hard, so it can't be the *front* end.

**This composes with C rather than competing.** C's private toolbelt is exactly where upstream MCP
servers would plug in — so D isn't really an option, it's an implementation choice for C's back end,
and one that can arrive whenever a capability is easier to borrow than to write.

---

## Feature menu

Grouped by how much they earn relative to what they cost.

### Tier 1 — the reason to build it

- **Calendar that actually reasons.** Not "create event" — *"find me 90 minutes with nobody else in
  it before Thursday and put the studio address on it."* Free/busy across calendars, conflict
  detection, moving things, invitees.
- **Capture-to-inbox with zero failure modes.** Whatever you say gets stored, always, even if
  everything downstream breaks. This is the actual product promise of the ring; it should be the one
  tool that cannot fail.
- **Lookups with real search.** OpenRouter + a search provider, answer in one paragraph, pushed to
  the watch.
- **Web app feed.** Every request, transcript, tool call, model, token count, cost, result — with a
  "this was wrong, here's what I meant" button that writes a correction into the eval set.

### Tier 2 — high value, moderate cost

- **Memory.** "What did I say about the enlarger?" Simplest answer is signet's own store — the feed
  already contains every transcript, so full-text search over it is nearly free. letta and mem0 are
  both already on the box if you'd rather delegate.
- **Home Assistant control** — you already run it; the tool is thin.
- **Push-back to the watch** via ntfy + the Pebble notification path.
- **Routing to your existing sinks**: TaskTrove for todos, karakeep for links, Forgejo issues for
  project notes.
- **Scheduled briefings** — morning agenda, evening "what did you capture today," via cron.
- **Cost + rate accounting** per token/client, since OpenRouter bills per call.

### Tier 3 — fun, later

- **Raw-audio path.** Take the ring's audio via webhook, re-transcribe on the Arc A770 with the
  wyoming-whisper you already run, and diff it against Parakeet's output — better accuracy on names
  and film-stock jargon.
- **Spoken replies** with kokoro-tts.
- **Contact-aware disambiguation** — resolve "Sarah" against a local contact list before the model sees it.
- **Voice-driven halideworks queries** — "what's the gamma on Portra 400 in that datasheet" hitting
  chromaforge's stock data. This is the one that's uniquely yours.
- **Eval harness.** Replay the last N real requests against a new model or prompt, diff the tool
  calls. Cheap to build once the feed exists, and it's what lets you swap models fearlessly.

---

## The web app

Small, boring, and doing four jobs:

1. **Feed** — every request end-to-end: transcript → tools called → arguments → result → cost →
   latency. This is 80% of the value; you cannot debug a voice assistant you can't see.
2. **Config** — tokens (create/revoke/scope), which tools each token sees, model selection per path,
   the editable system prompt and the `instructions` string the Pebble app injects.
3. **OAuth consent** — Google Calendar's installed-app flow needs somewhere to land. Also the
   natural home for any other upstream credential.
4. **Corrections** — mark a result wrong, say what should have happened, accumulate an eval set.

**Auth:** tinyauth (already running on :3006) as forward auth in Caddy, or Cloudflare Access on the
web hostname. Either is fine because a *browser* can do redirects — the constraint only applies to
`/mcp`. Keep them on separate hostnames so nothing that probes `/mcp` ever gets served HTML.

**Stack suggestion:** FastAPI + Jinja/htmx + SQLite (or the Postgres you already run). No SPA. This
is a dashboard, not a product.

---

## Deployment sketch

```
/srv/apps/signet/
  docker-compose.yml
  .env                        # OPENROUTER_API_KEY, GOOGLE_*, SIGNET_TOKENS, ...
  data/                       # sqlite + job queue + oauth tokens (encrypted at rest)

services:
  signet-api      :8xxx   FastAPI — /mcp (streamable), /sse (fallback), /webhook/index, /healthz
  signet-worker           background jobs for the async path
  (postgres/redis only if the queue justifies it — start with SQLite + a thread)
```

Exposure:

```
Cloudflare Tunnel (existing, token-managed → ingress edited in the Zero Trust dashboard)
  signet.example.com       -> caddy -> signet-api        # /mcp, no Access, bearer only
  signet-app.example.com   -> caddy -> signet-api (app)  # tinyauth or CF Access
```

Then in the Pebble app: URL `https://signet.example.com/mcp`, **streamable = true**, auth
header `Bearer <token>`. Tokens are per-client, so a second consumer later is just another row.

**Build order that de-risks the unknowns first:**

1. A do-nothing MCP server with one `capture` tool, behind the tunnel, with a bearer token.
2. Connect the **real Pebble app** to it. Confirm the `experimental` MCP settings screen is
   reachable on a shipping build, that a modern SDK can still serve its 2025-06-18 handshake, and
   that Streamable HTTP survives the tunnel. **If any of this fails, the whole design changes** — so
   it goes first, before a single feature is written.
3. Verify `_meta.coreSchema` + `semanticResult` actually renders in the feed. If it doesn't, we fall
   back to plain text and lose some polish, nothing more.
4. *Then* build features.

Optional, ten minutes, any time after step 1: point Claude Code at the same URL. It's a free check
that the server is standards-clean rather than accidentally Pebble-shaped — which is all "keep the
door open for hermes" really requires.

---

## Open questions for us to settle

> **Settled — see [`02-architecture.md`](02-architecture.md).** Kept here for the reasoning.

1. **Where does the thinking happen?** A, B, or C — and if C, how fat is the fast lane?
2. **Async or hard-bounded?** Is "I'll get back to you in 40 seconds via a notification" acceptable
   for the ring, or must every answer be synchronous and therefore small?
3. **Does signet own its memory, or delegate to letta/mem0?** Default answer: own it — the feed is
   already the corpus. Delegating is a later swap behind one internal tool.
4. **Where do todos/notes actually land?** signet's own store, TaskTrove, karakeep, Google Tasks —
   or does signet stay stateless and just relay?
5. **How much of the existing stack may signet touch?** Home Assistant, Emby, transmission, forgejo —
   a voice-triggered agent with a static token that can reach those is a meaningful blast radius.
6. **Cost ceiling.** Per-request and per-month caps on OpenRouter, and what happens when they're hit.
7. **Public or private repo, GitHub or forgejo?** The MCP dialect findings in `00-research.md` are
   genuinely useful to other Index 01 owners and would make a decent public write-up.

---

## Name

`signet` — a ring you press to authorize something. Fits the ring, fits the "seal of approval"
gesture, one word, matches the `voidwire`/`pneuma`/`acetate`/`isomorph` register.

Alternatives if it doesn't land: **bezel** (the ring's face), **carat**, **annulus**, **ferrule**,
**quire** (a gathering of leaves — leans toward the note-taking side), **hallmark** (the stamp a
signet leaves).
