"""The four verbs the ring sees.

Two themes run through these tests. Everything answers in band, because an MCP result is what
renders in the app feed and reaches the watch. And nothing the user says is ever dropped, even
when the model is missing, the budget is spent, or the feature is not built yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from signet import config, coreschema, db
from signet.auth import Principal
from signet.envelope import Outcome, Request
from signet.llm import LLM, Completion, LLMUnavailable
from signet.registry import Registry
from signet.router import Router
from signet.verbs import Verbs

RING_SCOPES = frozenset({"journal:write", "journal:read", "search:read"})


class FakeLLM(LLM):
    def __init__(self, text="It rained on Tuesday.", fail=False, data=None):
        super().__init__(api_key="test-key")
        self._text = text
        self._fail = fail
        self._data = data
        self.calls = 0

    async def complete(self, system, user, *, schema=None, max_tokens=600, timeout=None):
        self.calls += 1
        if self._fail:
            raise LLMUnavailable("down")
        return Completion(
            text=self._text,
            data=self._data,
            model="fake",
            tokens_in=100,
            tokens_out=20,
            cost_usd=0.0001,
        )


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    settings = config.Config(
        token="t" * 48, data_dir=tmp_path, host="127.0.0.1", port=0, daily_cost_cap_usd=2.0
    )
    config.set_cached(settings)
    return settings


@pytest.fixture
def conn(cfg: config.Config) -> sqlite3.Connection:
    connection = db.connect(cfg.db_path)
    db.migrate(connection)
    db.seed_token(connection, cfg.token)  # request rows reference a real token
    return connection


def build(cfg: config.Config, llm: LLM) -> Verbs:
    registry = Registry()
    registry.discover()
    return Verbs(cfg=cfg, registry=registry, llm=llm, router=Router(llm))


def ring_request(text: str) -> Request:
    return Request(
        text=text,
        source="mcp:ring",
        client=Principal(client_id="ring", scopes=RING_SCOPES, token_id=1),
    )


async def test_capture_saves_and_confirms(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    outcome = await verbs.call(conn, "capture", ring_request("buy fixer"))

    assert not outcome.is_error
    assert coreschema.headline(outcome.semantic) == "Noted to signet"
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == ["buy fixer"]


async def test_capture_needs_no_model(cfg, conn):
    """The recorder must work when everything else is down."""
    llm = FakeLLM(fail=True)
    verbs = build(cfg, llm)
    outcome = await verbs.call(conn, "capture", ring_request("note"))
    assert not outcome.is_error
    assert llm.calls == 0


async def test_every_request_is_recorded_for_the_feed(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    await verbs.call(conn, "capture", ring_request("x"))

    row = conn.execute("SELECT * FROM requests").fetchone()
    assert row["verb"] == "capture"
    assert row["status"] == "ok"
    assert row["latency_ms"] is not None


async def test_ask_answers_from_the_journal(cfg, conn):
    verbs = build(cfg, FakeLLM("The bulb blew on Tuesday."))
    await verbs.call(conn, "capture", ring_request("the enlarger bulb blew"))

    outcome = await verbs.call(conn, "ask", ring_request("what happened to the enlarger"))
    assert coreschema.headline(outcome.semantic)
    assert coreschema.headline(outcome.semantic) == "The bulb blew on Tuesday."


async def test_ask_records_cost_and_model(cfg, conn):
    verbs = build(cfg, FakeLLM())
    await verbs.call(conn, "ask", ring_request("anything?"))

    row = conn.execute("SELECT * FROM requests WHERE verb='ask'").fetchone()
    assert row["cost_usd"] == pytest.approx(0.0001)
    assert row["model"] == "fake"
    assert row["tokens_in"] == 100


async def test_ask_without_a_model_falls_back_to_search(cfg, conn):
    """Being told what you wrote beats being told the model is unavailable."""
    verbs = build(cfg, LLM(api_key=None))
    await verbs.call(conn, "capture", ring_request("the shutter is sticking"))

    outcome = await verbs.call(conn, "ask", ring_request("shutter"))
    assert not outcome.is_error
    assert "sticking" in coreschema.headline(outcome.semantic)


async def test_ask_with_no_model_and_no_notes_says_so(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    outcome = await verbs.call(conn, "ask", ring_request("anything at all"))
    assert "nothing" in coreschema.headline(outcome.semantic).lower()


async def test_ask_survives_the_model_failing_midway(cfg, conn):
    verbs = build(cfg, FakeLLM(fail=True))
    outcome = await verbs.call(conn, "ask", ring_request("hello"))
    assert not outcome.is_error
    assert coreschema.headline(outcome.semantic)


async def test_daily_cost_cap_stops_model_calls(cfg, conn):
    """The runaway-loop breaker. Over budget, ask degrades instead of spending."""
    llm = FakeLLM()
    verbs = build(cfg, llm)
    request_id = db.start_request(conn, text="x", source="api")
    db.finish_request(conn, request_id, status="ok", cost_usd=5.0)

    outcome = await verbs.call(conn, "ask", ring_request("hello"))
    assert llm.calls == 0
    assert not outcome.is_error


async def test_capture_still_works_over_budget(cfg, conn):
    verbs = build(cfg, FakeLLM())
    request_id = db.start_request(conn, text="x", source="api")
    db.finish_request(conn, request_id, status="ok", cost_usd=99.0)

    outcome = await verbs.call(conn, "capture", ring_request("x"))
    assert not outcome.is_error


async def test_schedule_saves_rather_than_dropping(cfg, conn):
    """Calendar is P2. A spoken commitment must not vanish in the meantime."""
    verbs = build(cfg, LLM(api_key=None))
    outcome = await verbs.call(conn, "schedule", ring_request("coffee with Sarah Friday at 3"))
    assert "saved to your journal" in outcome.output.lower()
    rows = [r["text"] for r in conn.execute("SELECT text FROM journal")]
    assert rows == ["coffee with Sarah Friday at 3"]


async def test_do_routes_by_rule_without_a_model(cfg, conn):
    llm = FakeLLM()
    verbs = build(cfg, llm)
    outcome = await verbs.call(conn, "do", ring_request("remember that the fixer is low"))
    assert not outcome.is_error
    assert llm.calls == 0
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == ["the fixer is low"]


async def test_do_records_its_plan(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    await verbs.call(conn, "do", ring_request("remember milk"))
    row = conn.execute("SELECT * FROM requests WHERE verb='do'").fetchone()
    assert row["status"] == "ok"


async def test_unknown_verb_is_refused(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    outcome = await verbs.call(conn, "fly", ring_request("fly"))
    assert outcome.is_error
    assert outcome.semantic["llmRecoverable"] is False


async def test_every_verb_returns_something_the_watch_can_render(cfg, conn):
    """The watch is the point. A verb that returns no semantic result shows nothing."""
    verbs = build(cfg, FakeLLM())
    for verb, text in (
        ("capture", "a note"),
        ("ask", "a question"),
        ("schedule", "coffee on Friday"),
        ("do", "remember something"),
    ):
        outcome = await verbs.call(conn, verb, ring_request(text))
        assert outcome.semantic.get("type"), f"{verb} returned nothing renderable"


async def test_portal_settings_override_the_environment(cfg, conn):
    """A key set in the admin portal applies on the next request, with no restart. That is
    the point of storing it in the database rather than only in .env."""
    verbs = build(cfg, LLM(api_key=None))

    assert verbs._web_available(conn) is False
    db.set_config(conn, "exa_api_key", "set-from-the-portal")
    assert verbs._web_available(conn) is True

    assert verbs._llm_for(conn).available is False
    db.set_config(conn, "openrouter_api_key", "set-from-the-portal")
    resolved = verbs._llm_for(conn)
    assert resolved.available is True
    assert resolved.model == cfg.model

    db.set_config(conn, "model", "another/model")
    assert verbs._llm_for(conn).model == "another/model"


async def test_portal_cost_cap_overrides_the_environment(cfg, conn):
    verbs = build(cfg, FakeLLM())
    request_id = db.start_request(conn, text="x", source="api")
    db.finish_request(conn, request_id, status="ok", cost_usd=3.0)

    # Env cap is 2.00, so the budget is already spent.
    assert verbs._budget_left(conn) is False

    db.set_config(conn, "daily_cost_cap_usd", "10.00")
    assert verbs._budget_left(conn) is True


class ScriptedLLM(LLM):
    """Answers a list of canned replies in order, so a two-step ask can be exercised."""

    def __init__(self, *replies: str):
        super().__init__(api_key="test-key")
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, system, user, *, schema=None, max_tokens=600, timeout=None):
        self.calls += 1
        text = self.replies.pop(0) if self.replies else ""
        return Completion(
            text=text, data=None, model="fake", tokens_in=100, tokens_out=20, cost_usd=0.00004
        )


async def test_journal_answer_never_searches_the_web(cfg, conn):
    """A search costs about 175 times what an answer costs, so questions the journal can
    answer must not touch the network."""
    db.set_config(conn, "exa_api_key", "would-work-if-called")
    llm = ScriptedLLM("The bulb blew on Tuesday.")
    verbs = build(cfg, llm)
    await verbs.call(conn, "capture", ring_request("the enlarger bulb blew on Tuesday"))

    outcome = await verbs.call(conn, "ask", ring_request("what happened to the enlarger"))

    assert coreschema.headline(outcome.semantic) == "The bulb blew on Tuesday."
    assert llm.calls == 1, "answering from the journal should take one call and no search"
    assert outcome.data["sources"] == []


async def test_web_search_only_after_the_model_asks_for_it(cfg, conn, monkeypatch):
    import httpx

    from signet.capabilities import web as web_cap
    from signet.search import Exa

    payload = {
        "results": [{"title": "T", "url": "https://x", "text": "The answer is 42."}],
        "costDollars": {"total": 0.007},
    }
    monkeypatch.setattr(
        web_cap,
        "Exa",
        lambda key: Exa(
            key, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ),
    )
    db.set_config(conn, "exa_api_key", "key")

    llm = ScriptedLLM("NEED_SEARCH", "It is 42.")
    verbs = build(cfg, llm)
    outcome = await verbs.call(conn, "ask", ring_request("what is the answer to everything"))

    assert llm.calls == 2
    assert coreschema.headline(outcome.semantic) == "It is 42."
    assert outcome.data["sources"]
    # Both model calls plus the search are billed to the request.
    assert outcome.cost_usd == pytest.approx(0.00004 * 2 + 0.007)


async def test_marker_never_leaks_to_the_watch(cfg, conn):
    """With no search configured the escape hatch must turn into plain words, not leak
    NEED_SEARCH onto the user's wrist."""
    verbs = build(cfg, ScriptedLLM("NEED_SEARCH"))
    outcome = await verbs.call(conn, "ask", ring_request("what is the price of gold"))

    assert "NEED_SEARCH" not in coreschema.headline(outcome.semantic)
    assert "know" in coreschema.headline(outcome.semantic).lower()


async def test_time_sensitive_questions_skip_the_stale_local_attempt(cfg, conn, monkeypatch):
    """Asked for the current MCP spec version, the model answered confidently from a year-old
    training set instead of asking to search. A wrong answer costs more than the search, so
    freshness is decided by rule rather than by trusting the model to own up."""
    import httpx

    from signet.capabilities import web as web_cap
    from signet.search import Exa

    payload = {
        "results": [
            {"title": "Spec", "url": "https://x", "text": "The current version is 2026-07-28."}
        ],
        "costDollars": {"total": 0.007},
    }
    monkeypatch.setattr(
        web_cap,
        "Exa",
        lambda key: Exa(
            key, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ),
    )
    db.set_config(conn, "exa_api_key", "key")

    # One reply only: if the local attempt ran, the assertion on call count catches it.
    llm = ScriptedLLM("The current version is 2026-07-28.")
    verbs = build(cfg, llm)
    outcome = await verbs.call(conn, "ask", ring_request("what is the current spec version?"))

    assert llm.calls == 1, "should go straight to search, not ask the model twice"
    assert "2026-07-28" in coreschema.headline(outcome.semantic)
    assert outcome.data["sources"]


@pytest.mark.parametrize(
    "question",
    [
        "what is the current version of the spec",
        "what is the latest release",
        "what is the weather today",
        "who won the match",
        "what is the price of silver",
        "what happened in the news this week",
    ],
)
def test_freshness_markers_are_recognised(question: str):
    from signet.verbs import looks_time_sensitive

    assert looks_time_sensitive(question)


@pytest.mark.parametrize(
    "question",
    [
        "what happened to the enlarger",
        "how do I mix ID-11",
        "what did I say about the scanner",
        "remind me what the shutter problem was",
    ],
)
def test_personal_questions_are_not_treated_as_time_sensitive(question: str):
    from signet.verbs import looks_time_sensitive

    assert not looks_time_sensitive(question)


async def test_notes_win_over_freshness_markers(cfg, conn):
    """If the journal has matching notes, answer from them. The user's own record of the
    current state of their own things beats a web search."""
    llm = ScriptedLLM("You booked it for Thursday.")
    verbs = build(cfg, llm)
    db.set_config(conn, "exa_api_key", "key")
    await verbs.call(conn, "capture", ring_request("booked the darkroom for Thursday evening"))

    outcome = await verbs.call(conn, "ask", ring_request("when is the darkroom booked currently"))

    assert llm.calls == 1
    assert outcome.data["sources"] == []


async def _queue_destructive(cfg, conn, verbs, text="unlock the front door"):
    from pydantic import BaseModel

    from signet.capability import Capability

    class Args(BaseModel):
        text: str

    ran = []

    async def handler(request, args):
        ran.append(args.text)
        return Outcome(output="Unlocked.", semantic=coreschema.response("Unlocked."))

    verbs.registry.register(
        Capability(
            name="home.unlock",
            description="Unlock the front door",
            schema=Args,
            handler=handler,
            scopes=("journal:write",),
            destructive=True,
        )
    )
    queued = await verbs.registry.invoke(conn, ring_request(text), "home.unlock", {"text": text})
    return ran, queued


async def test_saying_yes_approves_the_waiting_action(cfg, conn):
    """There is no way for signet to reach the phone first, so approval happens in band on the
    next press. The ring is already talking to signet constantly."""
    verbs = build(cfg, LLM(api_key=None))
    ran, _ = await _queue_destructive(cfg, conn, verbs)
    assert ran == []

    outcome = await verbs.call(conn, "capture", ring_request("yes"))

    assert ran == ["unlock the front door"]
    assert db.pending_approvals(conn) == []
    assert not outcome.is_error


async def test_saying_no_cancels_it(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    ran, _ = await _queue_destructive(cfg, conn, verbs)

    outcome = await verbs.call(conn, "capture", ring_request("no"))

    assert ran == []
    assert db.pending_approvals(conn) == []
    assert "cancelled" in coreschema.headline(outcome.semantic).lower()


async def test_a_yes_inside_a_sentence_is_not_consent(cfg, conn):
    """ "Yes, remember to call the lab" is a note. Reading it as approval for something
    destructive is the one mistake that must not happen."""
    verbs = build(cfg, LLM(api_key=None))
    ran, _ = await _queue_destructive(cfg, conn, verbs)

    await verbs.call(conn, "capture", ring_request("yes remember to call the lab"))

    assert ran == []
    assert len(db.pending_approvals(conn)) == 1, "still waiting"
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == [
        "yes remember to call the lab"
    ]


async def test_yes_with_nothing_waiting_is_just_a_note(cfg, conn):
    verbs = build(cfg, LLM(api_key=None))
    await verbs.call(conn, "capture", ring_request("yes"))
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == ["yes"]


async def test_two_waiting_approvals_refuse_to_guess(cfg, conn):
    """With two pending, "yes" does not say which, and guessing at something destructive is
    the one place guessing is unacceptable."""
    verbs = build(cfg, LLM(api_key=None))
    ran, _ = await _queue_destructive(cfg, conn, verbs)
    db.queue_approval(
        conn, capability="home.unlock", args={"text": "back door"}, title="Unlock the back door"
    )

    outcome = await verbs.call(conn, "capture", ring_request("yes"))

    assert ran == []
    assert len(db.pending_approvals(conn)) == 2
    assert "more than one" in coreschema.headline(outcome.semantic).lower()
