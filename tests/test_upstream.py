"""Remote MCP servers mounted as capabilities.

The point of this layer is that an integration costs no code. The point of these tests is that
it also costs no assumptions: signet cannot tell from a schema whether a tool reads a door
sensor or opens the door, so the defaults have to be the careful ones.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from signet import config, db, upstream
from signet.auth import Principal
from signet.envelope import Request
from signet.registry import Registry

TOOLS = [
    {"name": "get_state", "description": "Read an entity", "input_schema": {"type": "object"}},
    {
        "name": "turn_on_light",
        "description": "Turn a light on",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {"name": "unlock_door", "description": "Unlock", "input_schema": {"type": "object"}},
]


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    settings = config.Config(token="t" * 48, data_dir=tmp_path, host="127.0.0.1", port=0)
    config.set_cached(settings)
    return settings


@pytest.fixture
def conn(cfg: config.Config) -> sqlite3.Connection:
    connection = db.connect(cfg.db_path)
    db.migrate(connection)
    return connection


def add_home(conn, **kwargs) -> None:
    upstream.save_upstream(
        conn,
        name="home",
        url="https://home.example.com/mcp",
        auth_header="Bearer secret",
        **kwargs,
    )
    upstream.record_tools(conn, "home", TOOLS)


def caller(*scopes: str) -> Request:
    return Request(
        text="x", source="mcp:ring", client=Principal(client_id="ring", scopes=frozenset(scopes))
    )


@pytest.mark.parametrize(
    "name,read_only",
    [
        ("get_state", True),
        ("list_entities", True),
        ("search_areas", True),
        ("getState", True),
        ("listAreas", True),
        ("turn_on_light", False),
        ("unlock_door", False),
        ("delete_everything", False),
        ("send_message", False),
    ],
)
def test_only_obvious_readers_skip_approval(name: str, read_only: bool):
    """A schema does not say whether a tool acts. Anything not obviously a read needs a tap,
    because guessing wrong once is worse than the tap."""
    assert upstream.looks_read_only(name) is read_only


def test_mounted_tools_are_internal(conn):
    """The ring still sees four verbs. That constraint is the whole reason this design works,
    so mounting forty tools must not touch it."""
    add_home(conn)
    registry = Registry()
    registry.discover()
    upstream.mount(registry, conn)

    exposed = [c.name for c in registry.visible_to(frozenset({"mcp:home"}))]
    assert exposed == []
    assert registry.get("home.turn_on_light") is not None


def test_defaults_are_the_careful_ones(conn):
    add_home(conn)
    registry = Registry()
    upstream.mount(registry, conn)

    reader = registry.get("home.get_state")
    actor = registry.get("home.turn_on_light")

    assert reader.destructive is False
    assert actor.destructive is True, "an unknown action needs approval"
    # Untrusted until told otherwise, which narrows the agent loop after using it.
    assert reader.returns_untrusted is True
    assert actor.scopes == ("mcp:home",)


def test_trusting_a_server_is_explicit(conn):
    add_home(conn, trusted=True)
    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.get_state").returns_untrusted is False


def test_approve_all_overrides_the_heuristic(conn):
    add_home(conn, approve_all=True)
    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.get_state").destructive is True


def test_the_servers_own_schema_reaches_the_agent(conn):
    """Rebuilding a JSON Schema as a pydantic model here would be a compiler with its own
    bugs, so the server's schema is passed through untouched."""
    add_home(conn)
    registry = Registry()
    upstream.mount(registry, conn)

    schema = registry.get("home.turn_on_light").input_schema()
    assert schema["properties"]["entity_id"]["type"] == "string"
    assert schema["required"] == ["entity_id"]


def test_a_disabled_server_mounts_nothing(conn):
    add_home(conn)
    upstream.save_upstream(
        conn, name="home", url="https://home.example.com/mcp", auth_header="", enabled=False
    )
    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.get_state") is None


def test_a_server_that_is_down_keeps_its_tools(conn):
    """Booting must not depend on someone else's server being up, and a rebooting home
    automation box should not take signet's capabilities with it."""
    add_home(conn)
    upstream.record_failure(conn, "home", "connection refused")

    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.get_state") is not None
    assert upstream.get_upstream(conn, "home")["last_error"] == "connection refused"


def test_editing_without_a_header_keeps_the_old_one(conn):
    """Blank means keep, the same rule every other secret in the portal follows."""
    add_home(conn)
    upstream.save_upstream(conn, name="home", url="https://moved.example.com/mcp", auth_header="")

    row = upstream.get_upstream(conn, "home")
    assert row["auth_header"] == "Bearer secret"
    assert row["url"] == "https://moved.example.com/mcp"


async def test_the_scope_gates_the_whole_server(conn):
    """Revoking a token's access to your house should be one tick, not per tool."""
    add_home(conn)
    registry = Registry()
    upstream.mount(registry, conn)

    denied = await registry.invoke(conn, caller("journal:read"), "home.get_state", {})
    assert denied.is_error


async def test_an_action_queues_for_approval(conn):
    add_home(conn)
    registry = Registry()
    upstream.mount(registry, conn)

    outcome = await registry.invoke(
        conn, caller("mcp:home"), "home.unlock_door", {"entity_id": "lock.front"}
    )
    assert outcome.data["awaiting_approval"] is True
    pending = db.pending_approvals(conn)
    assert len(pending) == 1
    assert json.loads(pending[0]["payload_json"])["args"] == {"entity_id": "lock.front"}


def test_scopes_offered_include_every_server(conn):
    add_home(conn)
    scopes = upstream.known_scopes(conn)
    assert "mcp:home" in scopes
    assert "journal:write" in scopes


async def test_a_failing_call_says_which_server(conn, monkeypatch):
    add_home(conn)
    registry = Registry()
    upstream.mount(registry, conn)

    async def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(upstream, "call_tool", boom)
    outcome = await registry.invoke(conn, caller("mcp:home"), "home.get_state", {})

    assert outcome.is_error
    assert "home" in outcome.output
    assert "connection refused" in (outcome.error or "")


def test_a_choice_beats_the_guess(conn):
    """Being asked to approve a light is friction with no safety in it, so an explicit choice
    always wins over the name heuristic."""
    add_home(conn)
    upstream.set_policy(conn, "home", {"turn_on_light": "auto", "get_state": "approve"})

    registry = Registry()
    upstream.mount(registry, conn)

    assert registry.get("home.turn_on_light").destructive is False
    assert registry.get("home.get_state").destructive is True
    # Untouched tools keep the default.
    assert registry.get("home.unlock_door").destructive is True


def test_a_tool_can_be_switched_off(conn):
    add_home(conn)
    upstream.set_policy(conn, "home", {"unlock_door": "off"})

    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.unlock_door") is None
    assert registry.get("home.get_state") is not None


def test_a_choice_survives_approve_all(conn):
    """Requiring approval everywhere is a starting point, not a cage."""
    add_home(conn, approve_all=True)
    upstream.set_policy(conn, "home", {"turn_on_light": "auto"})

    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.turn_on_light").destructive is False
    assert registry.get("home.get_state").destructive is True


def test_nonsense_policy_values_are_ignored(conn):
    add_home(conn)
    upstream.set_policy(conn, "home", {"turn_on_light": "whatever"})

    registry = Registry()
    upstream.mount(registry, conn)
    assert registry.get("home.turn_on_light").destructive is True


def test_remounting_replaces_rather_than_duplicates(conn):
    """Changing what a tool may do takes effect on the next request, not the next restart."""
    from signet.registry import get_registry, reload_upstreams

    add_home(conn)
    reload_upstreams(conn)
    assert get_registry().get("home.turn_on_light").destructive is True

    upstream.set_policy(conn, "home", {"turn_on_light": "auto"})
    reload_upstreams(conn)

    assert get_registry().get("home.turn_on_light").destructive is False
    # Built-ins are untouched by a remount.
    assert get_registry().get("journal.write") is not None
