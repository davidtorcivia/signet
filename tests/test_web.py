"""The admin portal.

The security tests matter most. This process is reachable from the internet through the
tunnel with no reverse proxy in front, so an unauthenticated page here is an open admin panel,
not a convenience.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import httpx
import pytest

from signet import config, db
from signet.web import app as web_app

PASSWORD = "correct horse battery staple"


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    settings = config.load(
        {
            "SIGNET_TOKEN": "t" * 48,
            "SIGNET_DATA_DIR": str(tmp_path),
            "SIGNET_ADMIN_PASSWORD": PASSWORD,
        }
    )
    config.set_cached(settings)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    db.seed_token(conn, settings.token)
    conn.close()
    return settings


@pytest.fixture
async def client(cfg: config.Config):
    portal = web_app.build(cfg)
    transport = httpx.ASGITransport(app=portal)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://portal", follow_redirects=False
    ) as http:
        yield http


async def sign_in(client: httpx.AsyncClient) -> None:
    response = await client.post("/login", data={"password": PASSWORD})
    assert response.status_code == 303


def test_portal_is_not_mounted_without_a_password(tmp_path: Path):
    """Fail closed. No password means no portal at all, rather than an open one."""
    settings = config.load({"SIGNET_TOKEN": "t" * 48, "SIGNET_DATA_DIR": str(tmp_path)})
    assert web_app.build(settings) is None


@pytest.mark.parametrize("path", ["/", "/feed", "/journal", "/tokens"])
async def test_pages_require_a_session(client: httpx.AsyncClient, path: str):
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/login"


async def test_feed_rows_endpoint_requires_a_session(client: httpx.AsyncClient):
    """The htmx polling endpoint is a separate route and needs its own check, or the feed
    leaks to anyone who knows the path."""
    assert (await client.get("/feed/rows")).status_code == 401


async def test_mutating_routes_require_a_session(client: httpx.AsyncClient):
    assert (await client.post("/kill")).status_code == 303
    assert (await client.post("/tokens", data={"name": "x"})).status_code == 303


async def test_wrong_password_is_rejected(client: httpx.AsyncClient):
    response = await client.post("/login", data={"password": "wrong"})
    assert response.status_code == 401
    assert (await client.get("/")).status_code == 303


async def test_sign_in_then_browse(client: httpx.AsyncClient):
    await sign_in(client)
    for path in ("/", "/feed", "/journal", "/tokens"):
        assert (await client.get(path)).status_code == 200


async def test_dashboard_shows_counts(client: httpx.AsyncClient, cfg: config.Config):
    conn = db.connect(cfg.db_path)
    db.add_journal(conn, "the enlarger bulb blew")
    request_id = db.start_request(conn, text="hello", source="mcp:ring", verb="ask")
    db.finish_request(conn, request_id, status="ok", latency_ms=120, cost_usd=0.0004)
    conn.close()

    await sign_in(client)
    body = (await client.get("/")).text
    assert "Captured today" in body
    assert "0.0004" in body
    assert "ask" in body


async def test_feed_shows_what_was_said_and_the_answer(
    client: httpx.AsyncClient, cfg: config.Config
):
    conn = db.connect(cfg.db_path)
    request_id = db.start_request(conn, text="what did I say", source="mcp:ring", verb="ask")
    db.finish_request(
        conn, request_id, status="ok", result={"output": "You said the bulb blew."}, latency_ms=90
    )
    conn.close()

    await sign_in(client)
    body = (await client.get("/feed")).text
    assert "what did I say" in body
    assert "You said the bulb blew." in body


async def test_journal_search(client: httpx.AsyncClient, cfg: config.Config):
    conn = db.connect(cfg.db_path)
    db.add_journal(conn, "the darkroom timer needs a bulb")
    db.add_journal(conn, "buy more fixer")
    conn.close()

    await sign_in(client)
    body = (await client.get("/journal", params={"q": "timer"})).text
    assert "darkroom timer" in body
    assert "fixer" not in body


async def test_creating_a_token_shows_it_once(client: httpx.AsyncClient):
    await sign_in(client)
    await client.post("/tokens", data={"name": "phone"})

    first = (await client.get("/tokens")).text
    assert "Copy this now" in first
    assert "Bearer " in first

    # Second view must not show it again: it is only stored hashed.
    assert "Copy this now" not in (await client.get("/tokens")).text


async def test_revoking_a_token_stops_it_working(client: httpx.AsyncClient, cfg: config.Config):
    conn = db.connect(cfg.db_path)
    token_id, plaintext = db.create_token(conn, "temp", ["journal:write"])
    conn.close()

    await sign_in(client)
    assert (await client.post(f"/tokens/{token_id}/revoke")).status_code == 303

    conn = db.connect(cfg.db_path)
    assert db.lookup_token(conn, plaintext) is None
    conn.close()


async def test_kill_switch_toggles_and_persists(client: httpx.AsyncClient, cfg: config.Config):
    await sign_in(client)
    await client.post("/kill")

    conn = db.connect(cfg.db_path)
    assert db.kill_switch_on(conn) is True
    conn.close()

    await client.post("/kill")
    conn = db.connect(cfg.db_path)
    assert db.kill_switch_on(conn) is False
    conn.close()


async def test_pages_are_mobile_first(client: httpx.AsyncClient):
    """Not a rendering test, but the two things whose absence guarantees a bad phone
    experience: a viewport meta tag and touch-sized targets."""
    await sign_in(client)
    body = (await client.get("/")).text
    assert 'name="viewport"' in body
    assert "min-height: 44px" in body


async def test_htmx_is_served_locally(client: httpx.AsyncClient):
    """Bundled rather than from a CDN, so the dashboard works on a home server with no
    outbound access."""
    await sign_in(client)
    response = await client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert len(response.text) > 1000


async def test_settings_requires_a_session(client: httpx.AsyncClient):
    assert (await client.get("/settings")).status_code == 303
    assert (await client.post("/settings", data={"model": "x"})).status_code == 303


async def test_secrets_are_never_sent_to_the_browser(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "exa_api_key", "exa-super-secret-value")
    conn.close()

    await sign_in(client)
    body = (await client.get("/settings")).text
    assert "exa-super-secret-value" not in body
    assert "set, leave blank to keep" in body


async def test_saving_a_setting_takes_effect(client: httpx.AsyncClient, cfg):
    await sign_in(client)
    await client.post("/settings", data={"exa_api_key": "new-key", "model": "some/model"})

    conn = db.connect(cfg.db_path)
    assert db.get_config(conn, "exa_api_key") == "new-key"
    assert db.get_config(conn, "model") == "some/model"
    conn.close()


async def test_blank_secret_keeps_the_existing_value(client: httpx.AsyncClient, cfg):
    """The field is never prefilled, so an empty box means "unchanged", not "erase"."""
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "exa_api_key", "keep-me")
    conn.close()

    await sign_in(client)
    await client.post("/settings", data={"exa_api_key": "", "model": "m"})

    conn = db.connect(cfg.db_path)
    assert db.get_config(conn, "exa_api_key") == "keep-me"
    conn.close()


async def test_clearing_falls_back_to_the_environment(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "model", "override/model")
    conn.close()

    await sign_in(client)
    await client.post("/settings", data={"clear": "model"})

    conn = db.connect(cfg.db_path)
    assert db.get_config(conn, "model") is None
    conn.close()


async def test_rubbish_number_is_ignored(client: httpx.AsyncClient, cfg):
    await sign_in(client)
    await client.post("/settings", data={"daily_cost_cap_usd": "banana"})

    conn = db.connect(cfg.db_path)
    assert db.get_config(conn, "daily_cost_cap_usd") is None
    conn.close()


async def test_only_allowlisted_keys_are_settable(cfg):
    """The bearer token and admin password stay in .env. Locking yourself out of your own
    server through a web form would be a bad afternoon."""
    conn = db.connect(cfg.db_path)
    with pytest.raises(ValueError):
        db.set_config(conn, "admin_password", "hunter2")
    with pytest.raises(ValueError):
        db.set_config(conn, "token", "anything")
    conn.close()


SAMPLE_MODELS = {
    "data": [
        {
            "id": "deepseek/deepseek-v4-flash-0731",
            "name": "DeepSeek: V4 Flash",
            "context_length": 1000000,
            "pricing": {"prompt": "0.00000009", "completion": "0.00000018"},
            "supported_parameters": ["tools", "structured_outputs"],
        },
        {
            "id": "someone/chatty",
            "name": "Someone: Chatty",
            "context_length": 8000,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "supported_parameters": ["temperature"],
        },
    ]
}


@pytest.fixture
def catalogue(cfg):
    """Prime the caches so the settings page never touches the network in tests."""
    from signet import openrouter

    conn = db.connect(cfg.db_path)
    db.set_setting(conn, openrouter.CACHE_KEY, json.dumps(SAMPLE_MODELS))
    db.set_setting(conn, openrouter.CACHE_AT_KEY, str(time.time()))
    db.set_setting(
        conn,
        openrouter.PROVIDER_CACHE_PREFIX + "deepseek/deepseek-v4-flash-0731",
        json.dumps(["DeepInfra", "Fireworks", "Novita"]),
    )
    conn.close()


async def test_model_picker_lists_models_and_marks_the_current_one(
    client: httpx.AsyncClient, catalogue
):
    await sign_in(client)
    body = (await client.get("/settings")).text

    assert '<select id="model"' in body
    assert "DeepSeek: V4 Flash" in body
    assert "Someone: Chatty" in body
    # Structured-output models are grouped first, because routing and scheduling need them.
    assert body.index("Structured output") < body.index("Answers only")
    assert "$0.09/$0.18 per M" in body


async def test_provider_picker_lists_real_providers(client: httpx.AsyncClient, catalogue):
    await sign_in(client)
    body = (await client.get("/settings")).text
    for provider in ("DeepInfra", "Fireworks", "Novita"):
        assert provider in body
    assert 'name="provider_order"' in body


async def test_selecting_providers_builds_the_routing_block(
    client: httpx.AsyncClient, cfg, catalogue
):
    await sign_in(client)
    await client.post("/settings", data={"provider_order": ["Novita", "DeepInfra"]})

    conn = db.connect(cfg.db_path)
    stored = json.loads(db.get_config(conn, "provider"))
    conn.close()
    # Submitted order is the routing order, and no allow_fallbacks checkbox means locked down.
    assert stored == {"order": ["Novita", "DeepInfra"], "allow_fallbacks": False}


async def test_allowing_fallbacks_is_recorded(client: httpx.AsyncClient, cfg, catalogue):
    await sign_in(client)
    await client.post("/settings", data={"provider_order": "Novita", "allow_fallbacks": "1"})

    conn = db.connect(cfg.db_path)
    stored = json.loads(db.get_config(conn, "provider"))
    conn.close()
    assert stored == {"order": ["Novita"]}


async def test_no_providers_ticked_clears_the_preference(client: httpx.AsyncClient, cfg, catalogue):
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "provider", json.dumps({"order": ["Novita"]}))
    conn.close()

    await sign_in(client)
    await client.post("/settings", data={"allow_fallbacks": "1"})

    conn = db.connect(cfg.db_path)
    assert db.get_config(conn, "provider") is None
    conn.close()


async def test_choosing_a_model_is_saved(client: httpx.AsyncClient, cfg, catalogue):
    await sign_in(client)
    await client.post("/settings", data={"model": "someone/chatty"})

    conn = db.connect(cfg.db_path)
    assert db.get_config(conn, "model") == "someone/chatty"
    conn.close()


async def test_settings_page_survives_an_empty_catalogue(client: httpx.AsyncClient):
    """No network and no cache should degrade to a text box, not a broken page."""
    await sign_in(client)
    response = await client.get("/settings")
    assert response.status_code == 200
    assert 'id="model"' in response.text


async def test_dashboard_chart_always_spans_fourteen_days(client: httpx.AsyncClient, cfg):
    """Quiet days must render as zero bars. Skipping them would silently redraw the window
    as something narrower than it claims."""
    conn = db.connect(cfg.db_path)
    db.add_journal(conn, "one note today")
    conn.close()

    await sign_in(client)
    body = (await client.get("/")).text
    assert body.count('class="col"') == 14
    assert 'class="bar zero"' in body


async def test_pages_share_the_same_furniture(client: httpx.AsyncClient, catalogue):
    """Every page gets the masthead, both navs, and the pause control."""
    await sign_in(client)
    for path in ("/", "/feed", "/journal", "/tokens", "/settings"):
        body = (await client.get(path)).text
        assert 'class="mark"' in body, path
        assert 'class="wordmark"' in body, path
        assert 'action="/app/kill"' in body, path
        assert 'name="viewport"' in body, path


def test_p95_is_never_below_p50():
    """A verb with two calls reported a p95 lower than its p50, because truncating the index
    returns the minimum for small samples."""
    from signet.web.app import _percentile

    assert _percentile([1467, 1817], 0.95) == 1817
    assert _percentile([55], 0.95) == 55
    assert _percentile([], 0.95) == 0
    assert _percentile(list(range(1, 101)), 0.95) == 95
    for sample in ([5, 9], [1, 2, 3], [7] * 4, [10, 200, 30]):
        assert _percentile(sample, 0.95) >= statistics.median(sample)


async def test_journal_entries_can_be_edited(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    entry_id = db.add_journal(conn, "the enlarger bulb blew")
    conn.close()

    await sign_in(client)
    body = (await client.get("/journal")).text
    assert f"/app/journal/{entry_id}/edit" in body

    await client.post(f"/journal/{entry_id}/edit", data={"text": "the enlarger fuse blew"})

    conn = db.connect(cfg.db_path)
    assert db.get_journal(conn, entry_id)["text"] == "the enlarger fuse blew"
    conn.close()


async def test_deleting_offers_undo_and_keeps_the_note(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    entry_id = db.add_journal(conn, "deleted by mistake")
    conn.close()

    await sign_in(client)
    await client.post(f"/journal/{entry_id}/delete")

    body = (await client.get("/journal")).text
    assert "Undo" in body
    assert "deleted by mistake" not in body

    await client.post(f"/journal/{entry_id}/restore")
    assert "deleted by mistake" in (await client.get("/journal")).text


async def test_deleted_view_lists_and_purges(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    entry_id = db.add_journal(conn, "really going")
    db.delete_journal(conn, entry_id)
    conn.close()

    await sign_in(client)
    body = (await client.get("/journal", params={"show": "deleted"})).text
    assert "really going" in body
    assert "Delete for good" in body

    await client.post(f"/journal/{entry_id}/purge")
    conn = db.connect(cfg.db_path)
    assert db.get_journal(conn, entry_id) is None
    conn.close()


async def test_journal_mutations_require_a_session(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    entry_id = db.add_journal(conn, "protected")
    conn.close()

    for path in ("edit", "delete", "restore", "purge"):
        response = await client.post(f"/journal/{entry_id}/{path}", data={"text": "hacked"})
        assert response.status_code == 303
        assert response.headers["location"] == "/app/login"

    conn = db.connect(cfg.db_path)
    assert db.get_journal(conn, entry_id)["text"] == "protected"
    conn.close()


async def test_redirect_uri_is_https_behind_the_tunnel(client: httpx.AsyncClient, cfg):
    """The tunnel terminates TLS and forwards plain HTTP, so the request arrives looking like
    http even though the browser used https. Google rejects a plain-http redirect for a real
    hostname, so this must not follow the request scheme blindly."""
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "google_client_id", "cid")
    db.set_config(conn, "google_client_secret", "sec")
    conn.close()

    await sign_in(client)
    response = await client.post(
        "/google/connect", headers={"host": "signet.example.com", "x-forwarded-proto": "https"}
    )
    location = response.headers["location"]
    assert "redirect_uri=https%3A%2F%2Fsignet.example.com%2Fapp%2Fgoogle%2Fcallback" in location


async def test_redirect_uri_upgrades_a_plain_http_request(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "google_client_id", "cid")
    db.set_config(conn, "google_client_secret", "sec")
    conn.close()

    await sign_in(client)
    response = await client.post("/google/connect", headers={"host": "signet.example.com"})
    assert "redirect_uri=https%3A%2F%2Fsignet.example.com" in response.headers["location"]


async def test_configured_public_url_wins(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "google_client_id", "cid")
    db.set_config(conn, "google_client_secret", "sec")
    db.set_config(conn, "public_url", "https://ring.example.org/")
    conn.close()

    await sign_in(client)
    response = await client.post("/google/connect", headers={"host": "wrong.example.com"})
    assert (
        "redirect_uri=https%3A%2F%2Fring.example.org%2Fapp%2Fgoogle%2Fcallback"
        in (response.headers["location"])
    )


async def test_settings_shows_the_exact_redirect_uri(client: httpx.AsyncClient, cfg):
    """There is nothing to guess: the page prints what Google must be told."""
    await sign_in(client)
    body = (await client.get("/settings", headers={"host": "signet.example.com"})).text
    assert "https://signet.example.com/app/google/callback" in body


async def test_callback_path_matches_the_registered_route(cfg):
    """A drifted route would produce a URI Google accepts and signet 404s on."""
    from signet.web.app import CALLBACK_PATH, build

    portal = build(cfg)
    paths = {getattr(r, "path", None) for r in portal.routes}
    assert CALLBACK_PATH == "/app" + "/google/callback"
    assert "/google/callback" in paths


async def test_connect_needs_both_id_and_secret(client: httpx.AsyncClient, cfg):
    """With only the client ID set, Connect appeared and the flow died at the token exchange
    with "client_secret is missing", after the user had already granted access."""
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "google_client_id", "cid")
    conn.close()

    await sign_in(client)
    body = (await client.get("/settings")).text
    assert "client secret missing" in body
    assert 'action="/app/google/connect"' not in body

    # And blocked server side too, in case the page was stale.
    response = await client.post("/google/connect")
    assert response.headers["location"] == "/app/settings"
    assert "client secret" in (await client.get("/settings")).text


async def test_connect_offered_once_both_are_set(client: httpx.AsyncClient, cfg):
    conn = db.connect(cfg.db_path)
    db.set_config(conn, "google_client_id", "cid")
    db.set_config(conn, "google_client_secret", "secret")
    conn.close()

    await sign_in(client)
    assert 'action="/app/google/connect"' in (await client.get("/settings")).text
