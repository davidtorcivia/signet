"""Todos: actionable captures.

A todo is not a note: it has a lifecycle (open/done), a due date, priority,
and recurrence. Separate table so the web list can be fast without scanning
the journal. Voice input auto-creates todos via router rules + LLM classification.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .. import coreschema, db
from ..capability import Capability
from ..config import load_cached
from ..envelope import Outcome, Request


def _parse_due(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    # Accept already-ISO, or try to preserve as-is if parseable elsewhere; validate ISO here
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v
    except ValueError:
        # Let handler normalize: attempt to treat bare date YYYY-MM-DD as due at 09:00 UTC
        try:
            d = datetime.fromisoformat(v)
            return d.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None


class AddArgs(BaseModel):
    text: str = Field(description="What to do, in the user's own words.")
    due_at: str | None = Field(default=None, description="ISO-8601 due date/time, or null.")
    priority: int = Field(default=0, ge=0, le=2, description="0 normal, 1 high, 2 urgent.")
    recurrence: Literal["none", "daily", "weekly", "monthly", "yearly"] = Field(
        default="none", description="Repetition."
    )


class ListArgs(BaseModel):
    status: str | None = Field(default=None, description="open, done, or null for all.")
    query: str | None = Field(default=None, description="Search text.")
    limit: int = Field(default=20, ge=1, le=50)


class GetArgs(BaseModel):
    id: str = Field(description="Todo id.")


class UpdateArgs(BaseModel):
    id: str = Field(description="Todo id.")
    text: str | None = None
    due_at: str | None = None  # empty string clears
    priority: int | None = Field(default=None, ge=0, le=2)
    recurrence: Literal["none", "daily", "weekly", "monthly", "yearly"] | None = None


class ToggleArgs(BaseModel):
    id: str = Field(description="Todo id.")


async def add(request: Request, args: AddArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        try:
            todo_id = db.add_todo(
                conn,
                args.text,
                request_id=request.id or None,
                due_at=_parse_due(args.due_at),
                priority=args.priority,
                recurrence=args.recurrence,
            )
        except ValueError as exc:
            return Outcome(
                output=str(exc),
                semantic=coreschema.generic_failure(str(exc), llm_recoverable=True),
                is_error=True,
            )
        row = db.get_todo(conn, todo_id)
    finally:
        conn.close()
    headline = args.text[:80]
    due = f" due {args.due_at}" if args.due_at else ""
    rec = f" ({args.recurrence})" if args.recurrence != "none" else ""
    return Outcome(
        output=f"Added todo: {headline}{due}{rec}",
        semantic=coreschema.task_created(title=args.text[:120], deadline=args.due_at or "")
        if args.due_at
        else coreschema.list_item(args.text, list_used="todos"),
        data={"todo_id": todo_id, "todo": dict(row) if row else None},
    )


async def list_todos(request: Request, args: ListArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        status = args.status if args.status in ("open", "done") else None
        rows = db.list_todos(conn, status=status, query=args.query, limit=args.limit)
    finally:
        conn.close()
    if not rows:
        return Outcome(
            output="No todos found.",
            semantic=coreschema.action_logged("todos", "No todos found", success=False),
            data=[],
        )
    entries = [
        {
            "id": r["id"],
            "text": r["text"],
            "status": r["status"],
            "due_at": r["due_at"],
            "priority": r["priority"],
            "recurrence": r["recurrence"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    noun = "todo" if len(entries) == 1 else "todos"
    listing = "\n".join(
        f"- [{e['status']}] {e['text']}" + (f" due {e['due_at']}" if e["due_at"] else "")
        for e in entries
    )
    return Outcome(
        output=f"{len(entries)} {noun}:\n{listing}",
        semantic=coreschema.supporting_data(f"{len(entries)} {noun} found", assistive_only=True),
        data=entries,
    )


async def get(request: Request, args: GetArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        row = db.get_todo(conn, args.id)
    finally:
        conn.close()
    if row is None or row["deleted_at"] is not None:
        return Outcome(
            output="Todo not found.",
            semantic=coreschema.generic_failure("Todo not found.", llm_recoverable=False),
            is_error=True,
        )
    return Outcome(
        output=row["text"],
        semantic=coreschema.action_logged("todos", row["text"], success=True),
        data=dict(row),
    )


async def update(request: Request, args: UpdateArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        # due_at handling: None means unchanged, "" means clear, otherwise parse
        due: str | None = None
        due_provided = "due_at" in args.model_fields_set
        if due_provided:
            due = _parse_due(args.due_at) if args.due_at else ("" if args.due_at == "" else None)
            # If user passed a non-empty unparsable due, _parse_due returns None but we want error
            if args.due_at and due is None and args.due_at.strip():
                return Outcome(
                    output="Could not understand due date. Use ISO-8601.",
                    semantic=coreschema.generic_failure("Bad due date", llm_recoverable=True),
                    is_error=True,
                )
        ok = db.update_todo(
            conn,
            args.id,
            text=args.text,
            due_at=due if due_provided else None,  # type: ignore[arg-type]
            priority=args.priority,
            recurrence=args.recurrence,
        )
        row = db.get_todo(conn, args.id) if ok else None
    finally:
        conn.close()
    if not ok:
        return Outcome(
            output="Could not update todo.",
            semantic=coreschema.generic_failure("Could not update todo.", llm_recoverable=False),
            is_error=True,
        )
    return Outcome(
        output="Updated.",
        semantic=coreschema.action_logged("todos", "Updated", success=True),
        data=dict(row) if row else None,
    )


async def toggle(request: Request, args: ToggleArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        row = db.toggle_todo(conn, args.id)
    finally:
        conn.close()
    if row is None:
        return Outcome(
            output="Todo not found.",
            semantic=coreschema.generic_failure("Todo not found.", llm_recoverable=False),
            is_error=True,
        )
    verb = "Completed" if row["status"] == "done" else "Reopened"
    return Outcome(
        output=f"{verb}: {row['text']}",
        semantic=coreschema.action_logged("todos", f"{verb}: {row['text'][:60]}", success=True),
        data=dict(row),
    )


async def delete(request: Request, args: ToggleArgs) -> Outcome:
    conn = db.connect(load_cached().db_path)
    try:
        ok = db.delete_todo(conn, args.id)
    finally:
        conn.close()
    if not ok:
        return Outcome(
            output="Todo not found or already deleted.",
            semantic=coreschema.generic_failure("Todo not found.", llm_recoverable=False),
            is_error=True,
        )
    return Outcome(
        output="Deleted.",
        semantic=coreschema.action_logged("todos", "Deleted", success=True),
    )


CAPABILITIES = [
    Capability(
        name="todos.add",
        description=(
            "Add a todo/task. Use when user wants to remember to do something, needs to buy, "
            "call, fix, etc. Extract due date if spoken."
        ),
        schema=AddArgs,
        handler=add,
        scopes=("todos:write",),
        exposure="internal",
        tier="instant",
    ),
    Capability(
        name="todos.list",
        description="List and search todos. Use to answer what is left to do or find a task.",
        schema=ListArgs,
        handler=list_todos,
        scopes=("todos:read",),
        exposure="internal",
        tier="instant",
    ),
    Capability(
        name="todos.get",
        description="Get a single todo by id.",
        schema=GetArgs,
        handler=get,
        scopes=("todos:read",),
        exposure="internal",
        tier="instant",
    ),
    Capability(
        name="todos.update",
        description="Edit a todo's text, due date, priority, or recurrence.",
        schema=UpdateArgs,
        handler=update,
        scopes=("todos:write",),
        exposure="internal",
        tier="instant",
    ),
    Capability(
        name="todos.toggle",
        description="Mark a todo done or reopen it. Flips its status.",
        schema=ToggleArgs,
        handler=toggle,
        scopes=("todos:write",),
        exposure="internal",
        tier="instant",
    ),
    Capability(
        name="todos.delete",
        description="Delete a todo (soft delete, recoverable).",
        schema=ToggleArgs,
        handler=delete,
        scopes=("todos:write",),
        exposure="internal",
        tier="instant",
    ),
]
