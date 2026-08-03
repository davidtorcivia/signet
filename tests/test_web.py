"""The admin portal.

The security tests matter most. This process is reachable from the internet through the
tunnel with no reverse proxy in front, so an unauthenticated page here is an open admin panel,
not a convenience.
"""

from __future__ import annotations

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
    assert "captured today" in body
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
