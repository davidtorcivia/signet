"""The journal: write and search.

`journal.write` is the one capability that must never fail. It takes no model call, does no
parsing, and is the only thing still permitted when the kill switch is on. Everything else in
signet can break and the ring still works as a recorder.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import coreschema, db
from ..capability import Capability
from ..config import load_cached
from ..envelope import Outcome, Request


class WriteArgs(BaseModel):
    text: str = Field(description="The note to save, in the user's own words.")
    kind: str = Field(default="note", description="note, todo, or idea.")


class SearchArgs(BaseModel):
    query: str = Field(description="What to look for.")
    limit: int = Field(default=10, ge=1, le=50)


async def write(request: Request, args: WriteArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        entry_id = db.add_journal(conn, args.text, request_id=request.id or None, kind=args.kind)
    finally:
        conn.close()
    return Outcome(
        output="Saved.",
        # Headline reads "Noted to signet" and the note itself is the expanded detail.
        # Response would have shown the user the single word "Replied".
        semantic=coreschema.list_item(args.text, list_used="signet"),
        data={"journal_id": entry_id},
    )


async def search(request: Request, args: SearchArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        rows = db.search_journal(conn, args.query, limit=args.limit)
    finally:
        conn.close()

    if not rows:
        return Outcome(
            output=f"Nothing in the journal matches {args.query!r}.",
            semantic=coreschema.action_logged("signet", "Nothing found", success=False),
            data=[],
        )

    entries = [{"created_at": r["created_at"], "text": r["text"]} for r in rows]
    # `output` can be long because it goes to the model. The watch gets the count, because a
    # list of notes is unreadable on a watch face.
    listing = "\n".join(f"- {e['created_at']}: {e['text']}" for e in entries)
    noun = "note" if len(entries) == 1 else "notes"
    return Outcome(
        output=f"{len(entries)} {noun} matching {args.query!r}:\n{listing}",
        semantic=coreschema.supporting_data(f"{len(entries)} {noun} found", assistive_only=True),
        data=entries,
    )


CAPABILITIES = [
    Capability(
        name="journal.write",
        description=(
            "Save a note, thought, or reminder exactly as spoken. "
            "Use when the user wants something remembered or written down."
        ),
        schema=WriteArgs,
        handler=write,
        scopes=("journal:write",),
        exposure="internal",
        tier="instant",
    ),
    Capability(
        name="journal.search",
        description="Search everything the user has previously captured.",
        schema=SearchArgs,
        handler=search,
        scopes=("journal:read",),
        exposure="internal",
        tier="instant",
    ),
]
