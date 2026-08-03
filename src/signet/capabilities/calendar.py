"""Google Calendar.

The semantic results here matter more than usual. `CalendarEventCreation` is what makes a
confirmation render natively in the app feed and appear on the watch, which is the whole point
of saying "put coffee with Sarah on Friday" and getting a glanceable answer back.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field

from .. import coreschema, db, google
from ..capability import Capability
from ..config import load_cached
from ..envelope import Outcome, Request

logger = logging.getLogger("signet.calendar")


def _day(moment: datetime) -> str:
    # Built by hand rather than with %-d, which is a glibc extension and raises on Windows.
    return f"{moment.strftime('%a')} {moment.strftime('%d').lstrip('0')} {moment.strftime('%b')}"


def _clock(moment: datetime) -> str:
    hour = moment.strftime("%I").lstrip("0") or "12"
    minute = moment.strftime("%M")
    return f"{hour}{'' if minute == '00' else ':' + minute}{moment.strftime('%p').lower()}"


def when(iso: str) -> str:
    """A time a human can read at a glance on a watch.

    The API returns ISO-8601, which is right for the model and useless on a wrist. All-day
    events arrive as a bare date with no time part, so they render as just the day.
    """
    if not iso:
        return ""
    try:
        if len(iso) == 10:  # all-day, e.g. 2026-08-10
            return _day(datetime.strptime(iso, "%Y-%m-%d"))
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso

    now = datetime.now(moment.tzinfo)
    days_away = (moment.date() - now.date()).days
    if days_away == 0:
        return _clock(moment)
    if days_away == 1:
        return f"tomorrow {_clock(moment)}"
    if 0 < days_away < 7:
        return f"{moment.strftime('%a')} {_clock(moment)}"
    return f"{_day(moment)} {_clock(moment)}"


class ListArgs(BaseModel):
    days: int = Field(default=7, ge=1, le=60, description="How many days ahead to look.")


class CreateArgs(BaseModel):
    summary: str = Field(description="Event title, short and natural.")
    start: str = Field(description="Start time, ISO-8601 with offset.")
    end: str = Field(description="End time, ISO-8601 with offset.")
    location: str | None = Field(default=None)


def _credentials(conn: sqlite3.Connection) -> tuple[str, str]:
    cfg = load_cached()
    client_id = db.get_config(conn, "google_client_id") or ""
    client_secret = db.get_config(conn, "google_client_secret") or ""
    if not client_id or not client_secret:
        raise google.GoogleUnavailable("Google credentials are not set")
    del cfg
    return client_id, client_secret


def _not_connected() -> Outcome:
    message = "Calendar isn't connected. Connect it in signet's settings."
    return Outcome(
        output=message,
        semantic=coreschema.generic_failure(message, llm_recoverable=False),
        is_error=True,
    )


async def list_events(request: Request, args: ListArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        try:
            client_id, client_secret = _credentials(conn)
            token = await google.access_token(conn, client_id, client_secret)
        except google.GoogleUnavailable:
            return _not_connected()

        try:
            events = await google.Calendar(token).list_events(days=args.days)
        except google.GoogleUnavailable as exc:
            logger.warning("calendar list failed: %s", exc)
            return Outcome(
                output="I could not reach your calendar.",
                semantic=coreschema.generic_failure(
                    "Could not reach the calendar.", llm_recoverable=True
                ),
                is_error=True,
            )
    finally:
        conn.close()

    if not events:
        return Outcome(
            output=f"Nothing on the calendar for the next {args.days} days.",
            semantic=coreschema.response("Nothing scheduled."),
            data=[],
        )

    listing = "\n".join(f"- {e.start}: {e.summary}" for e in events)
    # The watch gets the next thing, because a whole agenda is unreadable on a watch face.
    nxt = events[0]
    return Outcome(
        # ISO for the model, something glanceable for the wrist.
        output=f"{len(events)} events:\n{listing}",
        semantic=coreschema.response(f"{nxt.summary}, {when(nxt.start)}"),
        data=[{"summary": e.summary, "start": e.start, "end": e.end} for e in events],
    )


async def create_event(request: Request, args: CreateArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        try:
            client_id, client_secret = _credentials(conn)
            token = await google.access_token(conn, client_id, client_secret)
        except google.GoogleUnavailable:
            return _not_connected()

        try:
            event = await google.Calendar(token).create_event(
                summary=args.summary,
                start=args.start,
                end=args.end,
                location=args.location,
            )
        except google.GoogleUnavailable as exc:
            logger.warning("event creation failed: %s", exc)
            return Outcome(
                output="I could not create that event.",
                semantic=coreschema.generic_failure(
                    "Could not create the event.", llm_recoverable=True
                ),
                is_error=True,
            )
    finally:
        conn.close()

    return Outcome(
        output=f"Created {event.summary} at {event.start}.",
        # The variant that renders as a real calendar item in the app feed and on the watch.
        semantic=coreschema.calendar_event(
            title=event.summary,
            start_time=event.start,
            end_time=event.end,
            location=event.location,
        ),
        data={"id": event.id, "link": event.html_link},
    )


CAPABILITIES = [
    Capability(
        name="calendar.list",
        description="Look at what is on the user's calendar in the next few days.",
        schema=ListArgs,
        handler=list_events,
        scopes=("calendar:read",),
        exposure="internal",
        tier="fast",
    ),
    Capability(
        name="calendar.create_event",
        description="Put an event on the user's calendar.",
        schema=CreateArgs,
        handler=create_event,
        scopes=("calendar:write",),
        exposure="internal",
        tier="fast",
        # Not destructive: creating an event is easily undone and blocking it behind an
        # approval tap would make the headline feature useless from a ring.
        destructive=False,
    ),
]
