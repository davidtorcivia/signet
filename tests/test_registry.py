"""The registry is where every rule that must not be forgotten lives.

The headline test is `test_adding_a_capability_touches_nothing_else`: it is the acceptance
criterion for the whole design. If a new capability ever needs edits to the router, the
registry, or the MCP layer, the seam is in the wrong place.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from signet import config, coreschema, db
from signet.auth import Principal
from signet.capability import Capability
from signet.envelope import Outcome, Request
from signet.registry import Registry


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    cfg = config.Config(token="t" * 48, data_dir=tmp_path, host="127.0.0.1", port=0)
    config.set_cached(cfg)
    connection = db.connect(cfg.db_path)
    db.migrate(connection)
    return connection


class EchoArgs(BaseModel):
    text: str


async def echo(request: Request, args: EchoArgs) -> Outcome:
    return Outcome(output=args.text, semantic=coreschema.response(args.text))


def make(name: str = "test.echo", **kwargs) -> Capability:
    options = {
        "name": name,
        "description": "Echo something back.",
        "schema": EchoArgs,
        "handler": echo,
        "scopes": ("journal:write",),
    }
    options.update(kwargs)
    return Capability(**options)


def caller(*scopes: str, limit: int = 60) -> Principal:
    return Principal(
        client_id="test", scopes=frozenset(scopes), token_id=1, rate_limit_per_min=limit
    )


def request_from(principal: Principal, text: str = "hello") -> Request:
    return Request(text=text, source="api", client=principal)


async def test_happy_path(conn: sqlite3.Connection):
    registry = Registry()
    registry.register(make())
    outcome = await registry.invoke(
        conn, request_from(caller("journal:write")), "test.echo", {"text": "hi"}
    )
    assert not outcome.is_error
    assert outcome.output == "hi"


async def test_unknown_capability_is_refused(conn: sqlite3.Connection):
    registry = Registry()
    outcome = await registry.invoke(conn, request_from(caller()), "nope", {})
    assert outcome.is_error
    assert outcome.semantic["llmRecoverable"] is False


async def test_missing_scope_is_refused(conn: sqlite3.Connection):
    registry = Registry()
    registry.register(make())
    outcome = await registry.invoke(
        conn, request_from(caller("journal:read")), "test.echo", {"text": "hi"}
    )
    assert outcome.is_error


async def test_capability_with_unmet_scope_is_invisible():
    registry = Registry()
    registry.register(make(exposure="mcp"))
    assert registry.visible_to(frozenset({"journal:write"}))
    assert registry.visible_to(frozenset({"journal:read"})) == []


async def test_internal_capabilities_never_reach_the_ring():
    """This is what keeps the tool list at four verbs while the toolbelt grows."""
    registry = Registry()
    registry.register(make("a.internal", exposure="internal"))
    registry.register(make("b.public", exposure="mcp"))
    names = [c.name for c in registry.visible_to(frozenset({"journal:write"}))]
    assert names == ["b.public"]


async def test_bad_arguments_are_recoverable(conn: sqlite3.Connection):
    """The on-device model should be told to try again, not that the world is broken."""
    registry = Registry()
    registry.register(make())
    outcome = await registry.invoke(
        conn, request_from(caller("journal:write")), "test.echo", {"wrong": 1}
    )
    assert outcome.is_error
    assert outcome.semantic["llmRecoverable"] is True


async def test_handler_exception_becomes_a_clean_failure(conn: sqlite3.Connection):
    async def boom(request: Request, args: EchoArgs) -> Outcome:
        raise RuntimeError("upstream is down")

    registry = Registry()
    registry.register(make(handler=boom))
    outcome = await registry.invoke(
        conn, request_from(caller("journal:write")), "test.echo", {"text": "hi"}
    )
    assert outcome.is_error
    # The ring must never see a stack trace or an internal hostname.
    assert "upstream" not in outcome.output


async def test_destructive_capability_queues_instead_of_running(conn: sqlite3.Connection):
    """The ring cannot answer "are you sure": no session, no elicitation, and it is usually in
    a pocket. So the call is parked with its arguments and runs on a deliberate tap."""
    ran = False

    async def dangerous(request: Request, args: EchoArgs) -> Outcome:
        nonlocal ran
        ran = True
        return Outcome(output="done", semantic=coreschema.response("done"))

    registry = Registry()
    registry.register(make(handler=dangerous, destructive=True))
    outcome = await registry.invoke(
        conn, request_from(caller("journal:write")), "test.echo", {"text": "hi"}
    )

    assert ran is False, "it must not execute before approval"
    assert outcome.data["awaiting_approval"] is True
    assert "approval" in coreschema.headline(outcome.semantic).lower()
    assert len(db.pending_approvals(conn)) == 1


async def test_an_approved_job_runs_once(conn: sqlite3.Connection):
    from signet.registry import run_approved

    calls = []

    async def dangerous(request: Request, args: EchoArgs) -> Outcome:
        calls.append(args.text)
        return Outcome(output="done", semantic=coreschema.response("done"))

    registry = Registry()
    registry.register(make(handler=dangerous, destructive=True))
    queued = await registry.invoke(
        conn, request_from(caller("journal:write")), "test.echo", {"text": "unlock"}
    )
    job_id = queued.data["job_id"]

    outcome = await run_approved(registry, conn, job_id)
    assert not outcome.is_error
    assert calls == ["unlock"]

    # A second tap, or a replayed link, must not run it again.
    again = await run_approved(registry, conn, job_id)
    assert again.is_error
    assert calls == ["unlock"]


async def test_approving_cannot_widen_what_was_asked(conn: sqlite3.Connection):
    """The scopes stored with the job are used, not the approver's, so a tap authorises
    exactly the request that was made."""
    from signet.registry import run_approved

    seen = {}

    async def dangerous(request: Request, args: EchoArgs) -> Outcome:
        seen["scopes"] = request.scopes
        return Outcome(output="done", semantic=coreschema.response("done"))

    registry = Registry()
    registry.register(make(handler=dangerous, destructive=True, scopes=("home:control",)))
    queued = await registry.invoke(
        conn, request_from(caller("home:control")), "test.echo", {"text": "hi"}
    )
    await run_approved(registry, conn, queued.data["job_id"])

    assert seen["scopes"] == frozenset({"home:control"})


async def test_an_expired_approval_will_not_run(conn: sqlite3.Connection):
    """A tap tomorrow on something asked for today is not consent."""
    from signet.registry import run_approved

    ran = False

    async def dangerous(request: Request, args: EchoArgs) -> Outcome:
        nonlocal ran
        ran = True
        return Outcome(output="done", semantic=coreschema.response("done"))

    registry = Registry()
    registry.register(make(handler=dangerous, destructive=True))
    job_id = db.queue_approval(
        conn,
        capability="test.echo",
        args={"text": "hi"},
        title="do the thing",
        scopes=["journal:write"],
        ttl_minutes=-1,
    )

    outcome = await run_approved(registry, conn, job_id)
    assert outcome.is_error
    assert ran is False
    assert db.pending_approvals(conn) == [], "an expired job is not offered"


async def test_a_denied_job_never_runs(conn: sqlite3.Connection):
    from signet.registry import run_approved

    ran = False

    async def dangerous(request: Request, args: EchoArgs) -> Outcome:
        nonlocal ran
        ran = True
        return Outcome(output="done", semantic=coreschema.response("done"))

    registry = Registry()
    registry.register(make(handler=dangerous, destructive=True))
    queued = await registry.invoke(
        conn, request_from(caller("journal:write")), "test.echo", {"text": "hi"}
    )
    assert db.decide_job(conn, queued.data["job_id"], "denied")

    outcome = await run_approved(registry, conn, queued.data["job_id"])
    assert outcome.is_error
    assert ran is False


async def test_kill_switch_blocks_everything_except_the_journal(conn: sqlite3.Connection):
    registry = Registry()
    registry.register(make("other.thing"))
    registry.register(make("journal.write"))
    db.set_setting(conn, "kill_switch", "on")

    blocked = await registry.invoke(
        conn, request_from(caller("journal:write")), "other.thing", {"text": "hi"}
    )
    assert blocked.is_error

    allowed = await registry.invoke(
        conn, request_from(caller("journal:write")), "journal.write", {"text": "hi"}
    )
    assert not allowed.is_error, "capture must survive the kill switch"


async def test_rate_limit_trips_and_is_recoverable(conn: sqlite3.Connection):
    registry = Registry()
    registry.register(make())
    principal = caller("journal:write", limit=2)

    for _ in range(2):
        ok = await registry.invoke(conn, request_from(principal), "test.echo", {"text": "hi"})
        assert not ok.is_error

    limited = await registry.invoke(conn, request_from(principal), "test.echo", {"text": "hi"})
    assert limited.is_error
    assert limited.semantic["llmRecoverable"] is True


async def test_duplicate_registration_is_rejected():
    registry = Registry()
    registry.register(make())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(make())


async def test_builtin_capabilities_are_discovered(conn: sqlite3.Connection):
    registry = Registry()
    found = registry.discover()
    assert "journal.write" in found
    assert "journal.search" in found


async def test_journal_capabilities_round_trip(conn: sqlite3.Connection):
    registry = Registry()
    registry.discover()
    principal = caller("journal:write", "journal:read")

    await registry.invoke(
        conn,
        request_from(principal),
        "journal.write",
        {"text": "the enlarger bulb blew"},
    )
    found = await registry.invoke(
        conn, request_from(principal), "journal.search", {"query": "enlarger"}
    )
    assert not found.is_error
    assert "enlarger bulb blew" in found.output
    # A list of notes is unreadable on a watch, so the watch gets a count instead.
    assert found.semantic["type"] == "SupportingData"


async def test_adding_a_capability_touches_nothing_else(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch
):
    """The acceptance test for the architecture.

    A capability package dropped on disk becomes callable with no edits anywhere else: no
    router change, no registry change, no MCP wiring.
    """
    package = tmp_path / "extra_caps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "weather.py").write_text(
        textwrap.dedent(
            """
            from pydantic import BaseModel
            from signet import coreschema
            from signet.capability import Capability
            from signet.envelope import Outcome, Request

            class Args(BaseModel):
                city: str

            async def handler(request: Request, args: Args) -> Outcome:
                return Outcome(
                    output=f"It is raining in {args.city}.",
                    semantic=coreschema.response(f"Raining in {args.city}."),
                )

            CAPABILITIES = [
                Capability(
                    name="weather.today",
                    description="Today's weather.",
                    schema=Args,
                    handler=handler,
                    scopes=("search:read",),
                    exposure="mcp",
                )
            ]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    registry = Registry()
    assert registry.discover("extra_caps") == ["weather.today"]

    outcome = await registry.invoke(
        conn, request_from(caller("search:read")), "weather.today", {"city": "London"}
    )
    assert outcome.output == "It is raining in London."
    # It is exposed, permission-checked, and watch-ready without touching anything else.
    assert [c.name for c in registry.visible_to(frozenset({"search:read"}))] == ["weather.today"]
    assert outcome.semantic["type"] == "Response"


def test_every_capability_declares_scopes_and_a_schema():
    """A capability with no scopes is one that cannot be denied. Catch that at import time
    rather than discovering it when the ring calls something it should not."""
    registry = Registry()
    registry.discover()
    for capability in registry.all():
        assert capability.scopes, f"{capability.name} declares no scopes"
        assert issubclass(capability.schema, BaseModel)
        assert capability.description.strip()


def test_capability_input_schema_is_json_schema():
    registry = Registry()
    registry.discover()
    schema = registry.get("journal.write").input_schema()
    assert schema["type"] == "object"
    assert "text" in schema["properties"]
    assert schema["required"] == ["text"]
