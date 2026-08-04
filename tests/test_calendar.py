"""Google Calendar, and the schedule verb that drives it.

The recurring theme: a spoken commitment must never vanish. Every failure path here still
files the words in the journal.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from signet import config, coreschema, db, google
from signet.auth import Principal
from signet.envelope import Request
from signet.llm import LLM, Completion
from signet.registry import Registry
from signet.router import Router
from signet.verbs import Verbs

EVENT = {
    "id": "abc123",
    "summary": "Coffee with Sarah",
    "start": {"dateTime": "2026-08-07T15:00:00+01:00"},
    "end": {"dateTime": "2026-08-07T16:00:00+01:00"},
    "htmlLink": "https://calendar.google.com/event?eid=abc123",
}


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    settings = config.Config(token="t" * 48, data_dir=tmp_path, host="127.0.0.1", port=0)
    config.set_cached(settings)
    return settings


@pytest.fixture
def conn(cfg: config.Config) -> sqlite3.Connection:
    connection = db.connect(cfg.db_path)
    db.migrate(connection)
    db.seed_token(connection, cfg.token)
    return connection


def connect_google(conn: sqlite3.Connection) -> None:
    db.set_config(conn, "google_client_id", "client-id")
    db.set_config(conn, "google_client_secret", "client-secret")
    db.set_setting(conn, google.REFRESH_KEY, "refresh-token")
    db.set_setting(conn, google.ACCESS_KEY, "access-token")
    db.set_setting(conn, google.EXPIRY_KEY, "99999999999")


class SchedulingLLM(LLM):
    """Returns a parsed event, the way a real structured-output call would."""

    def __init__(self, data=None):
        super().__init__(api_key="k")
        self._data = (
            data
            if data is not None
            else {
                "events": [
                    {
                        "summary": "Coffee with Sarah",
                        "start": "2026-08-07T15:00:00+01:00",
                        "end": "2026-08-07T16:00:00+01:00",
                        "all_day": False,
                        "location": None,
                    }
                ]
            }
        )

    async def complete(self, system, user, *, schema=None, max_tokens=600, timeout=None):
        return Completion(
            text="", data=self._data, model="fake", tokens_in=50, tokens_out=20, cost_usd=0.0002
        )


def patch_calendar(monkeypatch, handler) -> None:
    """Point the calendar client at a stub transport.

    `real` is captured before patching: `cal.google` is the google module itself, so replacing
    the attribute in place would make the replacement call itself.
    """
    from signet.capabilities import calendar as cal

    real = google.Calendar
    monkeypatch.setattr(
        cal.google, "Calendar", lambda token: real(token, transport=httpx.MockTransport(handler))
    )


def build(cfg, llm) -> Verbs:
    registry = Registry()
    registry.discover()
    return Verbs(cfg=cfg, registry=registry, llm=llm, router=Router(llm))


def ring(text: str) -> Request:
    return Request(
        text=text,
        source="mcp:ring",
        client=Principal(
            client_id="ring",
            scopes=frozenset({"journal:write", "journal:read", "calendar:read", "calendar:write"}),
            token_id=1,
        ),
    )


def test_authorize_url_requests_offline_access():
    """Without offline access and a forced consent screen Google returns no refresh token,
    and the ring stops working an hour later."""
    url = google.authorize_url("cid", "https://signet.example.com/app/google/callback", "st")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=st" in url
    assert "calendar.events" in url


async def test_create_event_returns_a_watch_renderable_result(conn, monkeypatch):
    connect_google(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(200, json=EVENT)

    patch_calendar(monkeypatch, handler)

    registry = Registry()
    registry.discover()
    outcome = await registry.invoke(
        conn,
        ring("x"),
        "calendar.create_event",
        {
            "summary": "Coffee with Sarah",
            "start": "2026-08-07T15:00:00+01:00",
            "end": "2026-08-07T16:00:00+01:00",
        },
    )

    assert not outcome.is_error
    # This variant is what renders as a calendar item in the app feed and on the watch.
    assert outcome.semantic["type"] == "CalendarEventCreation"
    assert outcome.semantic["title"] == "Coffee with Sarah"
    assert outcome.semantic["startTime"] == "2026-08-07T15:00:00+01:00"


async def test_capability_without_credentials_says_so(conn):
    registry = Registry()
    registry.discover()
    outcome = await registry.invoke(conn, ring("x"), "calendar.list", {"days": 7})
    assert outcome.is_error
    assert "connect" in outcome.output.lower()


async def test_schedule_without_a_connection_saves_the_request(cfg, conn):
    verbs = build(cfg, SchedulingLLM())
    outcome = await verbs.call(conn, "schedule", ring("coffee with Sarah Friday at 3"))

    assert "saved to your journal" in outcome.output.lower()
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == [
        "coffee with Sarah Friday at 3"
    ]


async def test_schedule_creates_a_real_event(cfg, conn, monkeypatch):
    connect_google(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=EVENT)

    patch_calendar(monkeypatch, handler)

    verbs = build(cfg, SchedulingLLM())
    outcome = await verbs.call(conn, "schedule", ring("coffee with Sarah Friday at 3"))

    assert not outcome.is_error
    assert outcome.semantic["type"] == "CalendarEventCreation"
    # Cost of the parsing call is attributed to the request.
    assert outcome.cost_usd == pytest.approx(0.0002)


async def test_unparseable_time_falls_back_to_the_journal(cfg, conn):
    connect_google(conn)
    verbs = build(
        cfg, SchedulingLLM(data={"summary": "", "start": "", "end": "", "location": None})
    )
    outcome = await verbs.call(conn, "schedule", ring("sometime whenever"))

    assert "saved to your journal" in outcome.output.lower()
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == ["sometime whenever"]


async def test_calendar_refusing_still_keeps_the_words(cfg, conn, monkeypatch):
    connect_google(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "insufficient permissions"})

    patch_calendar(monkeypatch, handler)

    verbs = build(cfg, SchedulingLLM())
    outcome = await verbs.call(conn, "schedule", ring("coffee with Sarah Friday at 3"))

    # Not flagged as a failed tool call: the request was handled, just not as asked. Flagging
    # it would invite the on-device model to retry, which would duplicate the journal note.
    assert not outcome.is_error
    assert outcome.error, "the reason still has to reach the feed"
    assert "saved to your journal" in coreschema.headline(outcome.semantic)
    assert [r["text"] for r in conn.execute("SELECT text FROM journal")] == [
        "coffee with Sarah Friday at 3"
    ]


async def test_calendar_capabilities_are_internal():
    registry = Registry()
    registry.discover()
    exposed = [c.name for c in registry.visible_to(frozenset({"calendar:read", "calendar:write"}))]
    assert "calendar.create_event" not in exposed


async def test_calendar_write_needs_its_scope(conn):
    connect_google(conn)
    registry = Registry()
    registry.discover()
    reader = Request(
        text="x",
        source="mcp:ring",
        client=Principal(client_id="ring", scopes=frozenset({"calendar:read"}), token_id=1),
    )
    outcome = await registry.invoke(
        conn, reader, "calendar.create_event", {"summary": "s", "start": "a", "end": "b"}
    )
    assert outcome.is_error


def test_times_are_readable_on_a_watch():
    """The API returns ISO-8601, which is right for the model and unreadable on a wrist."""
    from datetime import timedelta

    from signet.capabilities.calendar import when

    now = datetime.now().astimezone()

    today = now.replace(hour=20, minute=0, second=0, microsecond=0)
    assert when(today.isoformat()) == "8pm"

    tomorrow = today + timedelta(days=1)
    assert when(tomorrow.isoformat()) == "tomorrow 8pm"

    later = (today + timedelta(days=3)).replace(hour=9, minute=30)
    assert when(later.isoformat()) == f"{later.strftime('%a')} 9:30am"

    # All-day events arrive as a bare date and must not gain an invented time.
    assert when("2026-08-10") == "Mon 10 Aug"
    assert "T" not in when("2026-08-10")

    assert when("") == ""
    assert when("not a date") == "not a date"


async def test_list_puts_a_readable_time_on_the_watch(conn, monkeypatch):
    connect_google(conn)
    payload = {
        "items": [
            {
                "id": "1",
                "summary": "Nami Nori Williamsburg",
                "start": {"dateTime": "2026-08-06T20:00:00-04:00"},
                "end": {"dateTime": "2026-08-06T22:00:00-04:00"},
            }
        ]
    }
    patch_calendar(monkeypatch, lambda r: httpx.Response(200, json=payload))

    registry = Registry()
    registry.discover()
    outcome = await registry.invoke(conn, ring("what's on"), "calendar.list", {"days": 14})

    shown = coreschema.headline(outcome.semantic)
    assert "2026-08-06T20:00:00" not in shown, "raw ISO reached the watch"
    assert shown.startswith("Nami Nori Williamsburg,")
    # The model still gets the precise timestamp.
    assert "2026-08-06T20:00:00-04:00" in outcome.output


async def test_several_dates_become_several_events(cfg, conn, monkeypatch):
    """ "Holds for Adobe on the 21st, 22nd and 24th" is three events. Asked for one object the
    model either picked a single date or invented a span across all of them, and the reply
    came back unparseable."""
    made = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        made.append(body)
        return httpx.Response(
            200,
            json={
                "id": f"id{len(made)}",
                "summary": body["summary"],
                "start": body["start"],
                "end": body["end"],
            },
        )

    connect_google(conn)
    patch_calendar(monkeypatch, handler)

    verbs = build(
        cfg,
        SchedulingLLM(
            data={
                "events": [
                    {
                        "summary": "Adobe hold",
                        "start": f"2026-08-{day}",
                        "end": f"2026-08-{day}",
                        "all_day": True,
                        "location": None,
                    }
                    for day in ("21", "22", "24")
                ]
            }
        ),
    )
    outcome = await verbs.call(conn, "schedule", ring("Adobe holds Aug 21st, 22nd and 24th"))

    assert not outcome.is_error
    assert len(made) == 3
    assert outcome.data["created"] == 3
    assert "3 events" in coreschema.headline(outcome.semantic)


async def test_all_day_events_use_dates_not_times(cfg, conn, monkeypatch):
    """Google wants `date` rather than `dateTime`, and treats the end as exclusive. Sending
    the same day for both makes a zero-length event that appears nowhere."""
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "1", "summary": "Adobe hold", **sent})

    connect_google(conn)
    patch_calendar(monkeypatch, handler)

    registry = Registry()
    registry.discover()
    await registry.invoke(
        conn,
        ring("x"),
        "calendar.create_event",
        {
            "summary": "Adobe hold",
            "start": "2026-08-21",
            "end": "2026-08-21",
            "all_day": True,
        },
    )

    assert sent["start"] == {"date": "2026-08-21"}
    assert sent["end"] == {"date": "2026-08-22"}, "end date must be the following day"


async def test_partial_failure_still_reports_what_landed(cfg, conn, monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(403, json={"error": "nope"})
        body = json.loads(request.content)
        return httpx.Response(200, json={"id": "1", "summary": body["summary"], **body})

    connect_google(conn)
    patch_calendar(monkeypatch, handler)

    verbs = build(
        cfg,
        SchedulingLLM(
            data={
                "events": [
                    {
                        "summary": f"Hold {n}",
                        "start": f"2026-08-2{n}",
                        "end": f"2026-08-2{n}",
                        "all_day": True,
                        "location": None,
                    }
                    for n in (1, 2)
                ]
            }
        ),
    )
    outcome = await verbs.call(conn, "schedule", ring("two holds"))

    assert outcome.data["created"] == 1
    assert outcome.data["failed"] == 1
    assert "failed" in coreschema.headline(outcome.semantic)


@pytest.mark.parametrize(
    "data",
    [
        # What the model actually returned: "title" where the schema said "summary".
        {"events": [{"title": "Coffee", "start": "2026-08-07T15:00:00-04:00"}]},
        {"events": [{"name": "Coffee", "start_time": "2026-08-07T15:00:00-04:00"}]},
        # A single event without the wrapper.
        {"summary": "Coffee", "start": "2026-08-07T15:00:00-04:00"},
        # The wrapper holding one object rather than a list.
        {"events": {"summary": "Coffee", "when": "2026-08-07T15:00:00-04:00"}},
    ],
)
def test_events_are_read_out_of_whatever_shape_arrives(data):
    """Strictness throws away good answers. A reply with the right date under the wrong key
    is a correct answer in the wrong clothes, and rejecting it tells the user signet could
    not work out a time it understood perfectly."""
    from signet.verbs import normalise_events

    events = normalise_events(data)
    assert len(events) == 1
    assert events[0]["summary"] == "Coffee"
    assert events[0]["start"].startswith("2026-08-07")


def test_all_day_is_inferred_from_a_bare_date():
    from signet.verbs import normalise_events

    events = normalise_events({"events": [{"summary": "Adobe hold", "start": "2026-08-21"}]})
    assert events[0]["all_day"] is True
    assert events[0]["end"] == "2026-08-21"

    timed = normalise_events(
        {"events": [{"summary": "Coffee", "start": "2026-08-07T15:00:00-04:00"}]}
    )
    assert timed[0]["all_day"] is False


def test_junk_is_still_rejected():
    from signet.verbs import normalise_events

    assert normalise_events(None) == []
    assert normalise_events({}) == []
    assert normalise_events({"events": [{"summary": "no date"}]}) == []
    assert normalise_events({"events": ["not an object"]}) == []
