# Deploying signet to erebus

The runbook for P0a-4 in [`docs/03-implementation-plan.md`](docs/03-implementation-plan.md).

**signet is not fronted by Caddy.** cloudflared runs on the bare host under systemd
(`tunnel run --token`), so the tunnel points straight at `localhost:8300`. No reverse-proxy
stanza, no shared Docker network, nothing published on the LAN — signet keeps its own default
Docker network, which is what the blast-radius section of `docs/02-architecture.md` asks for.

Everything below except step 5 is done. Step 5 is David's.

---

## 1. Check the port is free

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep 8300
```

Expect no output. If 8300 is taken, pick another 83xx, change `SIGNET_PORT` and the published
port in `docker-compose.yml`, and record it under *Deviations* in the implementation plan.

## 2. Copy the app across

From the workstation, tracked files only — this excludes `.venv`, `data/`, and `.env` by
construction:

```bash
git archive HEAD | ssh erebus "mkdir -p /srv/apps/signet && tar -x -C /srv/apps/signet"
```

## 3. Generate the token

```bash
ssh erebus
cd /srv/apps/signet
python3 -c "import secrets; print('SIGNET_TOKEN=' + secrets.token_urlsafe(48))" > .env
chmod 600 .env
```

Keep this value somewhere you can retype it into the phone. It cannot be rotated from the app,
so it is one per client and revocable only here.

## 4. Bring it up and verify

```bash
docker compose up -d --build
docker compose ps                       # expect: healthy
curl -s localhost:8300/healthz
python3 scripts/legacy_handshake.py http://127.0.0.1:8300 "$(cut -d= -f2 .env)"
```

The handshake script must exit 0. If it fails here the problem is signet or the container, not
the tunnel.

## 5. Tunnel hostname — **David does this**

In the Cloudflare Zero Trust dashboard (ingress is managed remotely because cloudflared runs in
`--token` mode; the local `~/.config/cloudflared/config.yml` is ignored):

| Field | Value |
| --- | --- |
| Subdomain | `signet` |
| Domain | `example.com` |
| Service | `http://localhost:8300` |

Do **not** put a Cloudflare Access policy on this hostname — it would break the ring, which
cannot follow a redirect or send Access's two custom headers. Since Access is unavailable here,
the bearer token is the authentication; consider a WAF rule scoped to this hostname and keep
the name unguessable.

## 6. Verify through the tunnel

```bash
python3 scripts/legacy_handshake.py https://signet.example.com "$SIGNET_TOKEN"
```

Every check must pass **and** the `returned promptly` lines must stay in the tens of
milliseconds. A 30–60 s stall on `tools/call` means the edge is buffering — that is assumption
#2 in the plan's decision table failing, and it changes the architecture, so stop and report
rather than working around it.

Optional standards check, ten minutes:

```bash
claude mcp add signet --scope local --transport http https://signet.example.com/mcp \
  --header "Authorization: Bearer $SIGNET_TOKEN"
claude mcp list          # expect: signet ... ✔ Connected
```

## 7. Monitoring

Add a check to uptime-kuma (already on the box, :3001) against `localhost:8300/healthz` — it
needs no token by design.

---

## Then: P0b, the ring

With the tunnel verified, the remaining unknowns need the physical Index 01. See **P0b** in
[`docs/03-implementation-plan.md`](docs/03-implementation-plan.md): find the MCP settings
screen on the shipping build, add signet with **streamable = true** and auth header
`Bearer <token>` (the word `Bearer` must be typed — signet rejects a bare token, and
`test_token_without_bearer_scheme_is_rejected` documents that), then press the ring and confirm
the result renders natively in the feed rather than as a raw blob.

## Updating a deployed signet

```bash
git archive HEAD | ssh erebus "tar -x -C /srv/apps/signet"
ssh erebus "cd /srv/apps/signet && docker compose up -d --build"
```

`.env` and `data/` are untracked, so they survive.
