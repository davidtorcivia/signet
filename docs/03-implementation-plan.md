# Implementation plan

This is the executable version of [`02-architecture.md`](02-architecture.md), written so an
implementing agent can build signet without re-deriving decisions or guessing at details. Docs
00–02 are the *why*; this is the *what, in what order, verified how*.

**Read [`00-research.md`](00-research.md) §2 and §5 before writing any code.** Every constraint
there is load-bearing and several are counter-intuitive (the client throws on pagination, the
tunnel eats SSE, the app's auth is one verbatim header).

---

## Rules for the implementing agent

1. **Phases are gates, not labels.** Do not start a phase until the previous phase's exit
   criteria are checked off. Do not build P1 features "while waiting" for P0's ring-dependent
   checks — three of the four P0 assumptions can fail in ways that change the P1 design.
2. **Every task below has a Verify step. Run it.** A task is not done because the code exists;
   it is done when the Verify command produces the stated result.
3. **Don't invent patterns on erebus.** One directory at `/srv/apps/signet/` with a
   `docker-compose.yml`, fronted by the existing Caddy container. Copy the shape of
   `/srv/apps/mem0-mcp/` (an MCP server already deployed there) rather than designing
   a new layout.
4. **Git workflow:** work on a branch, commit locally, merge to master locally. Push only on explicit approval.
   The repo is public at `github.com/davidtorcivia/signet` under MIT. Host specifics stay out of
   it: real hostnames, IPs, and the server inventory live in `docs/erebus.local.md`, which is
   gitignored. Use the placeholders (`signet.example.com`, `/srv/apps/`) in anything committed.
5. **When a pinned choice below conflicts with reality** (port taken, package renamed, API
   moved), fix it, note the change at the bottom of this file under *Deviations*, and continue.
   When an *architectural* assumption fails (see the P0 decision table), stop and report —
   that's a design change, not a deviation.
6. **Scope discipline.** The non-goals in `02-architecture.md` are binding: no todo store, no
   embeddings/vector DB, no streaming responses, no multi-user, and nothing built for a second
   MCP client beyond staying standards-compliant.

---

## Pinned stack

Decisions the design docs left as suggestions, now fixed so nothing blocks on a choice:

| Thing | Pin | Notes |
| --- | --- | --- |
| Language | Python 3.12 | |
| Package manager | `uv` (`pyproject.toml`, `uv.lock`) | |
| MCP server | official `mcp` Python SDK, current 2.x | Must serve the **2025-06-18 legacy handshake** *and* the modern 2026-07-28 flow. The SDK ships both (`docs/protocol-versions.md` in the SDK repo). If the installed version can't negotiate 2025-06-18, pin an older SDK — see P0 decision table. |
| HTTP layer | Starlette/FastAPI app; SDK's Streamable HTTP ASGI app mounted at `/mcp` | Web app (P1) mounts on the same app under `/app`, but is served on a **separate hostname** via Caddy so `/mcp` never answers HTML. |
| Server responses | Single non-streamed JSON — **the SDK's JSON-response mode must be switched on** (`json_response=True` in 1.x; verify the flag name in the installed version) | The SDK's Streamable HTTP transport wraps POST responses in SSE framing *by default*, which is exactly what the tunnel buffers (`00-research.md` §5.1). Leaving the default is the single most likely way to silently reintroduce the buffering problem. No streaming, ever. |
| DB | SQLite (WAL mode) + FTS5, file at `data/signet.db` | Postgres/redis explicitly deferred. |
| Job queue (P2) | SQLite table + asyncio worker task in-process | No broker until it hurts. |
| Model | `deepseek/deepseek-v4-flash-0731` via OpenRouter | Not needed at all in P0. |
| Container | `python:3.12-slim`, non-root user, port **8300** | Before deploying, confirm 8300 is free on erebus: `docker ps --format '{{.Names}} {{.Ports}}' | grep 8300`. If taken, pick another 83xx and record the deviation. |
| Hostnames | `signet.example.com` → `/mcp` (bearer only, no Access) · `signet-app.example.com` → web app (tinyauth forward-auth) | Two hostnames is a hard rule, not taste — MCP clients probe for `application/json` and choke on HTML. **The `/mcp` hostname is not proxied by Caddy**: cloudflared runs on the bare host, so the tunnel points straight at `localhost:8300`. The P1 web app still goes through Caddy for tinyauth. |
| Push (P2) | existing ntfy at `ntfy.example.com` | |

Target repo layout (grows top-down; P0 only needs through `server.py`):

```
signet/
  pyproject.toml
  Dockerfile
  docker-compose.yml          # the erebus deploy file; also runs locally
  .env.example                # every env var, documented, no real values
  src/signet/
    __init__.py
    config.py                 # env parsing; fail fast on missing vars
    server.py                 # ASGI wiring: /mcp mount, /healthz, auth middleware
    auth.py                   # bearer check (constant-time), token registry, scopes
    db.py                     # SQLite init, schema migrations (plain numbered .sql)
    envelope.py               # Request envelope (P1)
    router.py                 # rules-then-model routing (P1)
    registry.py               # capability discovery/registration (P1)
    capabilities/
      __init__.py
      journal/                # first real capability (P1)
    outlets/                  # mcp result, ntfy, web feed (P1/P2)
    web/                      # FastAPI routes + Jinja templates (P1)
  tests/
    test_handshake_legacy.py  # the 2025-06-18 wire test — the most important test in the repo
    test_auth.py
    ...
  scripts/
    legacy_handshake.sh       # curl reproduction of the Pebble handshake (see appendix)
```

---

## P0 — the spike

Goal restated: one `capture` tool, a bearer token, reachable through the tunnel, proven against
the wire protocol the Pebble app actually speaks. P0 splits into **P0a (no ring required — do
now)** and **P0b (requires the ring in hand — a checklist, not code)**.

Three of the four unverified assumptions are testable without the ring, because the research
extracted the app's exact client behavior from source. Only "the MCP settings screen is reachable
on a shipping build" and "coreSchema renders in the feed" strictly need the device.

### P0a-1 · Scaffold

- `uv init`, add deps: `mcp`, `fastapi`, `uvicorn`; dev deps: `pytest`, `pytest-asyncio`,
  `httpx`, `ruff`.
- `config.py` reads `SIGNET_TOKEN` (one static token for P0; the token table arrives in P1)
  and `SIGNET_DATA_DIR`. Missing var → exit non-zero with a clear message.

**Verify:** `uv run python -c "import signet"` exits 0; `uv run ruff check .` clean. Local run
command for every later Verify step: `uv run uvicorn signet.server:app --port 8300`.

### P0a-2 · Minimal server

- Mount the SDK's Streamable HTTP app at `/mcp` **with JSON-response mode on** (see the stack
  table — the SSE-framed default defeats the whole tunnel strategy). Sessions stay stateful
  (the Kotlin client sends `Mcp-Session-Id`); it's the response *framing* that must be JSON.
  A `405` on `GET /mcp` is fine — the optional server-notification stream is unused and would
  only be buffered anyway. Plain `GET /healthz` → `{"status":"ok"}`, no auth.
- ASGI middleware on `/mcp` only: require `Authorization: Bearer <SIGNET_TOKEN>`, compare with
  `secrets.compare_digest`, reject with 401 JSON (never HTML) otherwise.
- One tool, `capture`:
  - Input schema: `{"text": {"type": "string"}}` — one required field, nothing else. The caller
    is a ~1B on-device model; every extra field is a way to fail.
  - Description (verbatim, keep it this short): *"Save a note, thought, or reminder exactly as
    spoken. Use when the user wants something remembered or written down."*
  - Handler: append `{id, received_at (UTC ISO), text}` to `data/journal.jsonl`. No LLM, no
    parsing, no failure modes beyond disk. Return in the **coreSchema result shape** (appendix B)
    with a `Response` semanticResult of "Saved." — P0b needs this to test rendering, and plain-text
    clients ignore `_meta`/`structuredContent` harmlessly.
- Serve `initialize.instructions`: *"signet stores what you say. Prefer the capture tool for
  anything the user wants remembered."* (Free system-prompt channel — `00-research.md` §2.)
- **Never return `nextCursor` from `tools/list`.** The Pebble client hits `TODO("Handle
  pagination")` and throws. Add a test asserting the field is absent.

**Verify:** all of —
1. `pytest` green, including `test_handshake_legacy.py`, which must drive the *exact* legacy
   wire sequence from appendix A (not the SDK's own client, which may silently negotiate
   modern) and assert: negotiated `protocolVersion == "2025-06-18"`, `serverInfo` present,
   `instructions` present, `tools/list` returns exactly one tool with **no** `nextCursor`,
   `tools/call capture` returns the appendix-B shape.
2. `scripts/legacy_handshake.sh http://localhost:8300` passes end-to-end (same assertions,
   from outside the process).
3. Wrong/missing token → 401 with a JSON body; `/healthz` → 200 without a token.

### P0a-3 · Modern-client cross-check

Point Claude Code at the local server:
`claude mcp add signet --transport http http://localhost:8300/mcp --header "Authorization: Bearer <token>"`
— then list and call `capture` from a Claude Code session.

**Verify:** the call succeeds and the line lands in `journal.jsonl`. This proves the server
speaks *both* eras — the whole reason the SDK choice matters.

### P0a-4 · Containerize and deploy to erebus

- `Dockerfile`: `python:3.12-slim`, non-root, `uv sync --frozen`, expose 8300.
- `docker-compose.yml`: bind `./data:/data`, `env_file: .env`, `restart: unless-stopped`,
  attach to the same Docker network Caddy proxies through (copy from mem0-mcp's compose),
  healthcheck hitting `/healthz`. No `docker.sock` mount — the existing webhook-server has one;
  signet must not.
- On erebus: `mkdir -p /srv/apps/signet`, copy compose + `.env`, `docker compose up -d`.
- Caddy: add a stanza to `/srv/apps/caddy/Caddyfile` for `signet.example.com`
  → `signet:8300`, mirroring an existing stanza; reload Caddy.
- Tunnel: **cloudflared runs in `--token` mode, so ingress lives in the Cloudflare Zero Trust
  dashboard — the local `~/.config/cloudflared/config.yml` is ignored.** Adding
  `signet.example.com` is a dashboard edit (public hostname → the Caddy origin, same as
  `ntfy.example.com`). This needs David logged into the dashboard; it is the one P0a step
  an agent can't do alone — prepare everything else, then ask.
- Generate the real token: `python -c "import secrets; print(secrets.token_urlsafe(48))"` into
  the erebus `.env` only (never committed).

**Verify:** `scripts/legacy_handshake.sh https://signet.example.com` passes **through the
tunnel**, and a stopwatch on the `tools/call` round-trip shows a normal response time (no
30–60 s stall — a stall means edge buffering, see the decision table). Re-run the Claude Code
check against the public URL. `curl https://signet.example.com/healthz` → 200.

**P0a exit criteria:** all four Verify blocks above pass. At this point two of the four
assumptions (legacy handshake served; Streamable HTTP survives the tunnel) are confirmed
without the ring.

### P0b · Ring-in-hand checklist *(blocked on hardware — confirm David has the ring first)*

The Index 01 entered mass production early 2026. **Ask David whether his ring has arrived
before treating P0b as actionable.** Until then, P0a can be fully complete and parked.

On the real Pebble app:

1. Find the MCP servers screen (it lives in the `experimental` module —
   `.../settings/mcp/McpServers.kt`). Record *how* it's reached (menu path, dev flag, hidden
   gesture) in this doc.
2. Add signet: URL `https://signet.example.com/mcp`, **streamable = true**, auth header
   `Bearer <token>` (the app uses the value verbatim — the word `Bearer` must be typed).
3. Press-and-hold, say "remember that the darkroom timer needs a new bulb", release.
4. Confirm: the request hit signet (log line + journal row), and the app's feed shows a
   rendered result — not a raw JSON blob — proving `coreSchema`/`semanticResult` renders.
5. Record which gesture routed to signet (single vs. double click-hold) and whether signet's
   tools were offered to the fast memory path, the big-agent path, or both (sandbox groups,
   `00-research.md` §2).

### P0 decision table — what to do when an assumption fails

| Assumption | If it fails | Severity |
| --- | --- | --- |
| SDK still serves the 2025-06-18 handshake | Pin the newest `mcp` 1.x/2.x release that does (check the SDK's `protocol-versions.md` and changelog); if none, write a shim translating the legacy `initialize` flow in front of the modern app. **Stop and report before writing a shim.** | Blocks everything |
| Streamable HTTP survives the tunnel | Confirm responses are single JSON (they should already be); test on LAN to isolate the tunnel; if the edge still stalls, options are a WAF/cache rule for the hostname or falling back to the webhook inlet as primary. **Stop and report — this changes the architecture.** | Blocks everything |
| MCP settings screen reachable on shipping build | The MCP inlet is dead for the ring. Pivot the primary inlet to the **webhook surface** (the app can fire every recording at a URL — `00-research.md` §1); signet's envelope design already treats inlets as interchangeable. **Stop and report — P1's router grows a transcript-triage job.** | Redesign of the inlet layer |
| `coreSchema` renders in the feed | Drop `_meta`/`structuredContent`, return plain text content. Purely cosmetic. Record it and move on. | Cosmetic |

---

## P1 — the spine

Gate: P0a complete; P0b complete **or** explicitly waived by David (e.g. ring delayed but he
wants the server-side spine built at the known risk of the settings-screen assumption).

Order matters — each task builds on the previous:

### P1-1 · SQLite schema + db.py

Numbered `.sql` migrations applied at startup (a `schema_version` pragma/table; no migration
framework). Initial schema:

```sql
tokens   (id, name, token_hash, scopes, created_at, revoked_at, rate_limit_per_min)
requests (id, received_at, source, client_id, text, plan_json, status,
          result_json, error, latency_ms, cost_usd)
journal  (id, request_id, created_at, text, kind)
journal_fts (fts5, content=journal, columns: text)
jobs     (id, request_id, created_at, run_after, status, payload_json, result_json)  -- used in P2
```

Tokens are stored **hashed** (SHA-256 is fine — they're 48-byte random secrets, not passwords);
lookup by hash, `secrets.compare_digest` on the hash. The P0 env-var token becomes a seed row on
first boot. The first migration also imports any existing rows from P0's `data/journal.jsonl`
into `journal`, then renames the file to `journal.jsonl.imported` — don't strand the spike-era
captures, and don't leave two write paths alive. Scopes are a JSON array of strings (`"journal:write"`, `"journal:read"`,
`"calendar:read"`, `"calendar:write"`, `"search:read"`, `"home:control"`, `"media:control"`,
`"admin"`). The ring's token starts with exactly `journal:write journal:read search:read
calendar:read calendar:write`.

**Verify:** migration runs idempotently (boot twice, schema_version stable); FTS insert/query
round-trips.

### P1-2 · Envelope + registry + capability contract

Implement `envelope.py` and `registry.py` per the shapes in `02-architecture.md` (the
`Request(...)` and `Capability(...)` blocks there are the spec — pydantic models, fields as
written). Discovery: `registry.py` imports every package under `capabilities/` and collects
module-level `CAPABILITIES: list[Capability]`. Enforcement lives in the registry, not in
handlers: scope check, per-token rate limit (`rate_limit_per_min` from the tokens table —
this is where the security-invariants bullet gets implemented), destructive→queue (P2; until
then destructive capabilities are rejected), tier→sync/async dispatch, audit row into
`requests`.

**The seam test from `02-architecture.md` is the acceptance test:** adding a capability
directory must require zero edits outside that directory. Write a test that registers a dummy
capability from a temp dir and calls it through the registry with both a sufficient and an
insufficient token.

### P1-3 · The four verbs + router

The MCP-exposed surface is exactly four tools — these are *inlet adapters* that build an
envelope and hand it to the router, not capabilities themselves:

| Verb | Contract | Budget |
| --- | --- | --- |
| `capture(text)` | → journal capability directly (deterministic rule; no LLM) | <200 ms |
| `ask(question)` | one DeepSeek call + optional search capability → `Response` | hard 45 s |
| `schedule(request)` | NL → calendar capability (P2 stubs it: journals the request, `Response` says "calendar isn't connected yet — saved it") | 45 s |
| `do(request)` | router → plan → capability or agent loop; if projected slow, return "On it — I'll ping you" `Response` and queue (P2) | sync path ≤60 s |

Tool descriptions: one sentence each, distinct verbs, no jargon — audience is a ~1B model.
Keep total tool count at 4; nothing else gets `exposure: "mcp"` without David's sign-off.

Router: `rules.yaml` (data, not code) evaluated first — ordered list of `{match: prefix|regex,
pattern, capability, args_template}`; unmatched text goes to one DeepSeek
`structured_outputs` call that returns a `Plan{capability, args}` or `{agent_loop: true}`.
Router must work with zero rules defined.

Cost circuit breaker (the "cost caps still go in" line from `02-architecture.md`), implemented
here because this task introduces the first model call: a `SIGNET_DAILY_COST_CAP_USD` env var
(default `2.00`); every OpenRouter call records its cost into `requests.cost_usd`; when the
day's sum exceeds the cap, model-backed paths return a `Response` of "Daily budget hit — capture
still works" instead of calling out. `capture` is deliberately unaffected — it must never fail.

**Verify:** unit tests routing canonical phrases ("remember that X" → journal, "what did I say
about Y" → search) with the DeepSeek call mocked; a live `capture` through the ring (or curl)
lands in `journal` + `requests` with latency recorded.

### P1-4 · search.journal capability

FTS5 `MATCH` over `journal_fts`, newest-first, cap 20 rows — then (per the "retrieval is more
expensive than context" decision) `ask`'s prompt includes the *last 14 days of journal*
wholesale plus FTS hits for older material. No embeddings.

**Verify:** capture three notes, `ask` a question whose answer is in note two, get it back.

### P1-5 · The admin portal

`/app` on the second hostname behind tinyauth (forward-auth stanza in Caddy — copy the existing
tinyauth pattern on the box; unlike `/mcp`, this one *is* proxied, because a browser can follow
a redirect). FastAPI + Jinja + htmx, no SPA, no build step.

**Mobile-first, not merely mobile-tolerant** (David, 2026-08-03). The realistic use is standing
somewhere with a phone, having just pressed the ring, wanting to know what signet did with it.
So: single-column by default, tap targets over hover affordances, no horizontal scrolling, no
table that only works at 1200px wide, and the feed readable without pinch-zoom. Desktop is the
progressive enhancement, not the baseline.

Pages:

- **Feed** — every request end to end, newest first: transcript → plan → capability calls and
  arguments → result → model → tokens → cost → latency. Auto-refresh via htmx polling. This is
  80% of the portal's value; you cannot debug a voice assistant you cannot see.
- **Dashboard** — the at-a-glance numbers: captures today / this week, requests by verb, spend
  today against `SIGNET_DAILY_COST_CAP_USD`, p50 and p95 latency by verb, error and
  approval-queue counts, last-seen-per-token. Enough to answer "is it healthy and what has it
  cost me" without reading the feed.
- **Journal** — browse and full-text search everything captured, since the journal is the
  corpus and the ring has no screen of its own.
- **Tokens** — create/revoke, show-once secret, scope checkboxes, per-token rate limit.
- **Kill switch** — one prominent toggle: while on, only `journal:*` capabilities are callable.
  Must be reachable in one tap from any page on a phone; that is the whole point of it.

Corrections UI and the approval queue arrive in P2 and slot into the Feed and Dashboard.

**Verify:** ring capture appears in the feed within seconds; revoking a token 401s the next
call (test with a second token, not the ring's); kill switch blocks a non-journal capability.

**P1 exit criteria:** ring → capture → journal → visible in feed; `ask` answers from the
journal; tokens and kill switch work; the dummy-capability seam test passes. This is the
"genuinely useful" bar from `02-architecture.md`.

---

## P2 — calendar and async *(build only after P1 exit criteria)*

1. **Job queue + worker.** `jobs` table + an asyncio worker task in the same process
   (compose `signet-worker` service only if in-process proves flaky). `do()` writes a job when
   its plan exceeds the sync budget.
2. **ntfy outlet.** POST to the existing ntfy with per-request topic; approval actions use
   [ntfy action buttons](https://docs.ntfy.sh/publish/#action-buttons) hitting signed
   one-time `/app/approve/<job>` URLs.
3. **Approval queue.** `destructive: true` capabilities queue + notify instead of executing;
   web app shows pending approvals. This unblocks every future scary capability.
4. **Google Calendar.** Installed-app OAuth: consent URL in the web app, refresh token
   encrypted at rest (Fernet key in `.env`), `calendar.create_event` / `calendar.list` /
   `calendar.find_time` capabilities; `schedule` verb stops stubbing. `CalendarEventCreation`
   semanticResult for the feed rendering.
5. **Corrections UI.** "This was wrong" button on feed rows; correction text stored alongside
   the request — that's the eval set accumulating.

**Verify:** `do("look up X and get back to me")` returns "on it" through the ring in <5 s and
the real answer arrives as an ntfy push; a destructive dummy capability requires the tap;
`schedule("coffee with Sarah Friday at 3")` creates a real calendar event and the feed shows
it rendered.

---

## P3 — the routing station *(sketch; plan in detail only when reached)*

`mcp_upstream` capability provider (mount a remote MCP server's tools as namespaced internal
capabilities — Home Assistant's MCP integration is the first target, behind the approval
queue), cron inlet for briefings, rules editor in the web app. Everything after P3 must be
a capability, not a change to signet's core.

---

## Security invariants (all phases — check at every code review)

- `/mcp` returns JSON for every status including errors; HTML never.
- Untrusted fetched text (search results, any future email/message content) is fenced in
  prompts, and a turn whose context contains it cannot call capabilities beyond `journal:*`
  without the approval queue.
- Outbound HTTP allowlist in the capability layer: OpenRouter, Google, ntfy (LAN), Home
  Assistant (LAN), search provider. Anything else fails closed.
- Per-token rate limit + a global daily action cap (env-configured) as the runaway-loop breaker.
- Secrets only in erebus `.env`; repo carries `.env.example` only. No `docker.sock`. Read-only
  container FS except `/data`.

---

## Appendix A — legacy handshake wire test

This is what the Pebble app (MCP Kotlin SDK 0.8.3, Streamable HTTP mode) sends. The test and
`scripts/legacy_handshake.sh` must reproduce it literally — do not substitute a modern client.

```bash
# 1. initialize — expect 200, protocolVersion "2025-06-18" echoed, capture Mcp-Session-Id header
curl -s -D - https://signet.example.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18",
        "capabilities":{},
        "clientInfo":{"name":"pebble-sim","version":"0.8.3"}}}'

# 2. notifications/initialized — same headers plus Mcp-Session-Id: <captured>; expect 202/200
# 3. tools/list  — expect one tool, and assert "nextCursor" is ABSENT from the result
# 4. tools/call capture {"text":"handshake test"} — expect the appendix-B result shape
```

Assert on **every** POST response: `Content-Type: application/json` — *not*
`text/event-stream`. This is the assertion that catches the SDK's SSE-framed default (see the
stack table) before it ever reaches the tunnel; without it the local tests pass and the
deployed server stalls.

The pytest version asserts, additionally: the negotiated version in the response body is
exactly `2025-06-18` (not silently upgraded), and `instructions` is present in the
`initialize` result.

## Appendix B — coreSchema result shape

```json
{
  "content": [{ "type": "text", "text": "Saved." }],
  "structuredContent": {
    "output": "Saved.",
    "semanticResult": { "type": "<discriminator>", "text": "Saved." }
  },
  "_meta": { "coreSchema": 1 }
}
```

**Verified 2026-08-03** against `ToolCallResult.kt` and `HttpMcpIntegration.kt` in
[coredevices/mobileapp](https://github.com/coredevices/mobileapp). Implemented in
`src/signet/coreschema.py`; asserted in `tests/test_handshake_legacy.py`.

- **Discriminator key is `"type"`** — kotlinx.serialization's default. No
  `@JsonClassDiscriminator` on the sealed class and no `classDiscriminator` override at the
  decode site, which uses a plain `Json.decodeFromJsonElement`.
- **Variant names are the `@SerialName` strings**, which match the Kotlin class names:
  `Response` (`text`, `question?`), `SupportingData` (`summary`, `assistiveOnly`,
  `question?`), `GenericFailure` (`userErrorMessage`, `llmRecoverable`, `forceFallbackTool`),
  and the creation variants listed in `00-research.md` §2.
- **Both `output` and `semanticResult` are mandatory** whenever `_meta.coreSchema` is present.
  The app reads them with `getValue()`, which *throws* on a missing key — so a partial result
  is worse than no `_meta` at all.
- **`_meta.coreSchema` must be an integer.** A non-integer raises
  `error("coreSchema meta field is not an integer")` on the phone. Version ≤ 1 is the
  supported branch; a higher number logs a warning and falls back to a generic result.
- **Unknown keys are rejected.** The decode uses default `Json`, i.e. `ignoreUnknownKeys` is
  *false*, so an extra field in `semanticResult` fails on the phone rather than being ignored.
  Emit exactly the fields the variant declares — `test_semantic_result_carries_no_unknown_keys`
  guards this.

## Status

**P1 is complete and deployed (2026-08-03).** 103 tests green. Live behind the tunnel with
four verbs, SQLite + FTS5, scoped tokens, Exa web search, and the admin portal at `/app`.
Remaining for P2: Google Calendar, the approval queue, and the job queue.

**P0a was deployed and running on erebus (2026-08-03).** 17 tests green locally; the container
is up and healthy at `/srv/apps/signet/`, survives a restart, and passes every check in
`scripts/legacy_handshake.py` against `http://127.0.0.1:8300`. The Claude Code CLI reports
`✔ Connected`. Remaining: David points the tunnel at `localhost:8300`
([`DEPLOY.md`](../DEPLOY.md) step 5), then step 6 verifies through it — and then P0b, which
needs the ring.

Assumption status against the P0 decision table:

| Assumption | Status |
| --- | --- |
| SDK still serves the 2025-06-18 handshake | **Confirmed.** `mcp` 2.0.0 lists `2025-06-18` in both `SUPPORTED_PROTOCOL_VERSIONS` and `HANDSHAKE_PROTOCOL_VERSIONS`; the server negotiates it exactly, on the wire, without upgrading. No version pinning needed. The same server also serves modern clients via `server/discover` (`tests/test_modern_client.py`). |
| Streamable HTTP survives the tunnel | **Confirmed 2026-08-03.** Full handshake through `https://signet.example.com`: every response single JSON, 50–62 ms end to end, no buffering and no stall. The design's biggest fork resolves the way it was hoped. |
| MCP settings screen reachable on shipping build | **Not yet testable** — needs the ring (P0b). |
| `coreSchema` renders in the feed | **Wire format confirmed from source** (appendix B); rendering itself still needs the ring (P0b). |

## Deviations

*(record here any pinned choice that had to change, with one line of why)*

- **`scripts/legacy_handshake.sh` → `scripts/legacy_handshake.py`.** Stdlib-only Python
  instead of bash: no `jq` dependency, and it runs unchanged on Windows and on erebus. It is
  the check you run against the tunnel from whatever box you happen to be on, so portability
  beat matching the original filename. Output is deliberately ASCII — Windows consoles are
  cp1252 and choked on the arrow and em-dash characters.
- **`mcp` 2.0.0, unpinned.** The plan allowed for pinning an older SDK if the current one had
  dropped the handshake era. It hasn't, so the dependency stays `mcp>=1.9`.
- **Dev Python is 3.13 via `.python-version` 3.12 pin.** `uv` fetches 3.12 to match the
  container image; the host's own 3.13 is not used for the venv.
- **`ruff` excludes `docs/`.** `ruff format` reformats Python code blocks inside Markdown and
  silently rewrote a hand-aligned snippet in `02-architecture.md`. Design docs are prose.
- **The `Dockerfile` copies `README.md`.** `pyproject.toml` declares it as the project readme,
  and hatchling validates metadata during the in-container build.
- **No Caddy stanza; the tunnel points straight at `localhost:8300`.** cloudflared runs on the
  bare host under systemd, so the reverse-proxy hop bought nothing. signet keeps its own
  default Docker network and publishes on loopback only — closer to the blast-radius intent in
  `02-architecture.md` than the original Caddy plan was. (For the record, the external network
  named `caddy` that the compose file originally assumed does not exist on erebus; Caddy sits
  on `media-server_default`. Moot now.)
- **Cloudflare 403s the `Python-urllib` User-Agent at the edge.** Verified 2026-08-03 on the
  live hostname: `Python-urllib/3.12` → 403 before the request reaches the tunnel, while
  `curl`, an absent UA, and **`Ktor client` — what the Pebble app sends — all → 200.** So the
  ring is unaffected, but `scripts/legacy_handshake.py` now sets an explicit `User-Agent` or it
  reports a tunnel-wide failure that looks exactly like a rejected token. Worth remembering
  before adding any Python-based client or monitor against this hostname.
- **The container runs as the host uid, not the image's `signet` user.** The image creates uid
  10001, but the bind-mounted `./data` is owned by uid 1000, so the first deploy came up
  healthy and then threw `PermissionError` on every capture. `user: "${SIGNET_UID:-1000}:
  ${SIGNET_GID:-1000}"` fixes it and keeps the journal readable by David. Two guards were
  added so this class of failure cannot be silent again: `config.load()` write-probes the data
  dir at boot and refuses to start, and `legacy_handshake.py` now fails on a missing `result`
  or an `isError` instead of skipping its assertions.
