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
    "You turn a spoken scheduling request into a calendar event. Today's date and the user's "
    "timezone are given. Resolve relative dates like 'Friday' or 'tomorrow' against them. "
    "Default to a one hour duration when none is stated. Keep the title short and natural."
)

DEFAULTS = {
    "prompt_answer": ANSWER,
    "prompt_router": ROUTER,
    "prompt_schedule": SCHEDULE,
}


def get(conn: sqlite3.Connection, key: str) -> str:
    """The active prompt: whatever the portal holds, else the default."""
    return db.get_config(conn, key) or DEFAULTS[key]
