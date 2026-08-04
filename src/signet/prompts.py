"""Editable prompts.

Voice and tone belong to the person using it, not to whoever wrote the code, so these are
editable in the admin portal. The defaults live here and remain visible in the portal, so a
bad edit is always one click from being undone.

The one part of the answer prompt worth keeping whatever else changes: brevity. These answers
are read on a watch face.
"""

from __future__ import annotations

import sqlite3

from . import db

ANSWER = (
    "You answer questions for someone reading the reply on a smart watch. "
    "Answer in one or two short sentences. No preamble, no markdown, no lists. "
    "If the answer is in the provided notes, use them. If you do not know, say so briefly."
)

ROUTER = (
    "You route a voice request to exactly one tool. Reply with the tool name and its "
    "arguments. If nothing fits, use journal.write to save the text verbatim. Never invent a "
    "tool that is not listed."
)

SCHEDULE = (
    "You turn a spoken scheduling request into calendar events. Today's date and the user's "
    "timezone are given; resolve relative dates like 'Friday' or 'tomorrow' against them.\n"
    "Return one entry per date mentioned. Three dates means three entries.\n"
    "Set all_day true when no time of day is given, or when the request says hold, block, "
    "booked out, away, or all day. For an all-day entry use a bare date, YYYY-MM-DD, with no "
    "time. Only invent a clock time when the user actually gave one.\n"
    "Otherwise default to one hour. Keep titles short and natural."
)

DEFAULTS = {
    "prompt_answer": ANSWER,
    "prompt_router": ROUTER,
    "prompt_schedule": SCHEDULE,
}


def get(conn: sqlite3.Connection, key: str) -> str:
    """The active prompt: whatever the portal holds, else the default."""
    return db.get_config(conn, key) or DEFAULTS[key]
