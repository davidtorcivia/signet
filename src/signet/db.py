"""SQLite access and schema migrations.

Deliberately no ORM and no migration framework. The schema is small, the queries are simple,
and a numbered directory of .sql files is easier to read a year from now than a migration DSL.

Concurrency: WAL mode with a busy timeout. One writer at a time is plenty here, and the whole
point of the P0 journal design carries over, which is that writes must not fail.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_TABLE = "schema_version"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    return {row["name"] for row in conn.execute(f"SELECT name FROM {SCHEMA_TABLE}")}


def _migration_files() -> list[tuple[str, str]]:
    files = resources.files("signet.migrations")
    out = [
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in files.iterdir()
        if entry.name.endswith(".sql")
    ]
    return sorted(out, key=lambda pair: pair[0])


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations. Idempotent: safe to run on every boot.

    The bookkeeping INSERT is appended to the script and the whole thing wrapped in one
    BEGIN/COMMIT, because `executescript` commits any transaction already open. Doing it any
    other way leaves a window where the schema is applied but unrecorded, and the next boot
    would try to apply it again.
    """
    done = _applied(conn)
    ran: list[str] = []
    for name, sql in _migration_files():
        if name in done:
            continue
        # Both values are controlled here: a filename from the package and a generated
        # timestamp. Nothing user-supplied reaches this string.
        record = f"INSERT INTO {SCHEMA_TABLE}(name, applied_at) VALUES ('{name}', '{now_iso()}');"
        conn.executescript(f"BEGIN;\n{sql}\n{record}\nCOMMIT;")
        ran.append(name)
    return ran


def import_legacy_journal(conn: sqlite3.Connection, jsonl_path: Path) -> int:
    """Pull P0's append-only journal.jsonl into the journal table, once.

    P0 wrote captures to a flat file before this table existed. Those are real notes the ring
    took, so they get imported rather than stranded, and the file is renamed afterwards so
    there is only ever one live write path.
    """
    if not jsonl_path.exists():
        return 0

    imported = 0
    with transaction(conn):
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            existing = conn.execute(
                "SELECT 1 FROM journal WHERE id = ?", (record["id"],)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO journal(id, request_id, created_at, text, kind) VALUES (?,?,?,?,?)",
                (
                    record["id"],
                    None,
                    record.get("received_at") or now_iso(),
                    record["text"],
                    record.get("kind", "note"),
                ),
            )
            imported += 1

    jsonl_path.rename(jsonl_path.with_suffix(".jsonl.imported"))
    return imported


# --- tokens ---------------------------------------------------------------------------

DEFAULT_RING_SCOPES = [
    "journal:write",
    "journal:read",
    "search:read",
    "calendar:read",
    "calendar:write",
    "todos:read",
    "todos:write",
]


def seed_token(conn: sqlite3.Connection, token: str, name: str = "ring") -> int:
    """Register the env-provided token as a real row, so P1 has one code path for auth."""
    digest = hash_token(token)
    row = conn.execute("SELECT id FROM tokens WHERE token_hash = ?", (digest,)).fetchone()
    if row:
        return int(row["id"])
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO tokens(name, token_hash, scopes, created_at) VALUES (?,?,?,?)",
            (name, digest, json.dumps(DEFAULT_RING_SCOPES), now_iso()),
        )
    return int(cur.lastrowid)


def create_token(conn: sqlite3.Connection, name: str, scopes: list[str]) -> tuple[int, str]:
    """Returns (id, plaintext). The plaintext is shown once and never stored."""
    token = secrets.token_urlsafe(48)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO tokens(name, token_hash, scopes, created_at) VALUES (?,?,?,?)",
            (name, hash_token(token), json.dumps(scopes), now_iso()),
        )
    return int(cur.lastrowid), token


def lookup_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()


def touch_token(conn: sqlite3.Connection, token_id: int) -> None:
    conn.execute("UPDATE tokens SET last_seen_at = ? WHERE id = ?", (now_iso(), token_id))


def revoke_token(conn: sqlite3.Connection, token_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE tokens SET revoked_at = ? WHERE id = ?", (now_iso(), token_id))


# --- settings -------------------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def kill_switch_on(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "kill_switch", "off") == "on"


# --- requests and journal -------------------------------------------------------------


def start_request(
    conn: sqlite3.Connection,
    *,
    text: str,
    source: str,
    verb: str | None = None,
    client_id: int | None = None,
) -> str:
    request_id = new_id()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO requests(id, received_at, source, verb, client_id, text) "
                "VALUES (?,?,?,?,?,?)",
                (request_id, now_iso(), source, verb, client_id, text),
            )
    except sqlite3.IntegrityError:
        # The client_id no longer resolves, most likely a token deleted mid-flight. Record
        # the request without an owner rather than failing: the audit trail is important, but
        # not more important than not dropping what the user said.
        with transaction(conn):
            conn.execute(
                "INSERT INTO requests(id, received_at, source, verb, client_id, text) "
                "VALUES (?,?,?,?,?,?)",
                (request_id, now_iso(), source, verb, None, text),
            )
    return request_id


def finish_request(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    status: str,
    result: Any = None,
    error: str | None = None,
    latency_ms: int | None = None,
    plan: Any = None,
    cost_usd: float = 0.0,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE requests SET status=?, result_json=?, error=?, latency_ms=?, plan_json=?, "
            "cost_usd=?, model=?, tokens_in=?, tokens_out=? WHERE id=?",
            (
                status,
                json.dumps(result) if result is not None else None,
                error,
                latency_ms,
                json.dumps(plan) if plan is not None else None,
                cost_usd,
                model,
                tokens_in,
                tokens_out,
                request_id,
            ),
        )


def add_journal(
    conn: sqlite3.Connection,
    text: str,
    *,
    request_id: str | None = None,
    kind: str = "note",
) -> str:
    entry_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO journal(id, request_id, created_at, text, kind) VALUES (?,?,?,?,?)",
            (entry_id, request_id, now_iso(), text, kind),
        )
    return entry_id


_FTS_TOKEN = re.compile(r"[A-Za-z0-9']+")


# Question words and glue. A spoken question is mostly these, and requiring a note to contain
# them is how "what happened to the enlarger?" fails to match a note that says the enlarger
# blew. Kept deliberately short: this is about question scaffolding, not general stopwording.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "about",
        "be",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "said",
        "say",
        "tell",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


def to_fts_query(query: str) -> str:
    """Turn free speech into a valid FTS5 expression.

    Two failures to avoid, both of which read as "signet forgot" rather than "bad query":

    - Raw punctuation is either a syntax error or silently matches nothing.
    - ANDing every word means a spoken question almost never matches, because the note does
      not contain the question's scaffolding. That is worse than it sounds: `ask` treats an
      empty journal result as grounds to run a paid web search, so bad matching quietly costs
      money for questions the journal could have answered.

    So: drop the scaffolding, OR what remains, and let bm25 rank. OR plus ranking beats AND
    here because recall matters more than precision when the corpus is one person's notes.
    """
    tokens = [t for t in _FTS_TOKEN.findall(query.lower()) if len(t) > 1]
    content = [t for t in tokens if t not in STOPWORDS]
    # A question made entirely of stopwords ("what did I say?") still deserves a search.
    terms = content or tokens
    return " OR ".join(f'"{term}"' for term in terms)


def search_journal(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 match, newest first. Empty query returns nothing rather than everything."""
    expression = to_fts_query(query)
    if not expression:
        return []
    try:
        return list(
            conn.execute(
                "SELECT j.* FROM journal_fts f JOIN journal j ON j.rowid = f.rowid "
                # bm25 first, so the note that best matches wins over the one that merely
                # shares a common word; recency breaks ties. Deleted rows stay out of the
                # corpus entirely, so a note you removed cannot come back as an answer.
                "WHERE journal_fts MATCH ? AND j.deleted_at IS NULL "
                "ORDER BY f.rank, j.created_at DESC LIMIT ?",
                (expression, limit),
            )
        )
    except sqlite3.OperationalError:
        # Belt and braces: a tokenizer change should degrade to a scan, not a 500.
        return list(
            conn.execute(
                "SELECT * FROM journal WHERE text LIKE ? AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            )
        )


def recent_journal(conn: sqlite3.Connection, days: int = 14, limit: int = 200) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM journal WHERE created_at >= datetime('now', ?) "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (f"-{days} days", limit),
        )
    )


def spend_today(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM requests "
        "WHERE received_at >= strftime('%Y-%m-%dT00:00:00Z', 'now')"
    ).fetchone()
    return float(row["total"])


# --- runtime configuration ------------------------------------------------------------
#
# Settings the admin portal can change without a redeploy. A value here wins over the
# environment, so `.env` is the bootstrap and the portal is the day-to-day control. Stored
# in the same SQLite file as everything else: it sits next to `.env` on the same disk with
# the same permissions, so encrypting it here would move the problem rather than solve it.

CONFIG_PREFIX = "config:"

# Only these may be set from the portal. SIGNET_TOKEN and SIGNET_ADMIN_PASSWORD are
# deliberately absent: locking yourself out of your own server through a web form is a bad
# afternoon, and the bearer token has its own managed lifecycle in the tokens table.
CONFIGURABLE = {
    "openrouter_api_key": "secret",
    "exa_api_key": "secret",
    "model": "text",
    "provider": "json",
    "model_params": "json",
    "daily_cost_cap_usd": "number",
    "prompt_answer": "prompt",
    "prompt_router": "prompt",
    "prompt_schedule": "prompt",
    "public_url": "text",
    "google_client_id": "text",
    "google_client_secret": "secret",
}


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    value = get_setting(conn, CONFIG_PREFIX + key, "")
    return value or None


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in CONFIGURABLE:
        raise ValueError(f"{key} is not settable at runtime")
    set_setting(conn, CONFIG_PREFIX + key, value)


def clear_config(conn: sqlite3.Connection, key: str) -> None:
    """Drop the override so the environment value applies again."""
    set_setting(conn, CONFIG_PREFIX + key, "")


def list_journal(
    conn: sqlite3.Connection, *, limit: int = 100, deleted: bool = False
) -> list[sqlite3.Row]:
    clause = "IS NOT NULL" if deleted else "IS NULL"
    order = "deleted_at" if deleted else "created_at"
    return list(
        conn.execute(
            f"SELECT * FROM journal WHERE deleted_at {clause} ORDER BY {order} DESC LIMIT ?",
            (limit,),
        )
    )


def get_journal(conn: sqlite3.Connection, entry_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()


def update_journal(conn: sqlite3.Connection, entry_id: str, text: str) -> bool:
    """Correct a note. The FTS index follows via the AFTER UPDATE trigger, so a fixed note is
    immediately findable by its new wording and no longer by the old."""
    text = text.strip()
    if not text:
        return False
    with transaction(conn):
        cur = conn.execute(
            "UPDATE journal SET text = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (text, now_iso(), entry_id),
        )
    return cur.rowcount > 0


def delete_journal(conn: sqlite3.Connection, entry_id: str) -> bool:
    """Soft delete: out of the corpus, still recoverable."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE journal SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now_iso(), entry_id),
        )
    return cur.rowcount > 0


def restore_journal(conn: sqlite3.Connection, entry_id: str) -> bool:
    with transaction(conn):
        cur = conn.execute("UPDATE journal SET deleted_at = NULL WHERE id = ?", (entry_id,))
    return cur.rowcount > 0


def purge_journal(conn: sqlite3.Connection, entry_id: str) -> bool:
    """Actually gone. Only reachable from the deleted view, never from the main list."""
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM journal WHERE id = ? AND deleted_at IS NOT NULL", (entry_id,)
        )
    return cur.rowcount > 0


# --- approval queue ---------------------------------------------------------------------
#
# Destructive capabilities never run when asked. The ring has no way to answer "are you sure",
# so the request is parked here with everything needed to run it later, and one tap in the
# portal carries it out.

AWAITING = "awaiting_approval"
APPROVAL_TTL_MINUTES = 60


def queue_approval(
    conn: sqlite3.Connection,
    *,
    capability: str,
    args: dict[str, Any],
    title: str,
    request_id: str | None = None,
    scopes: list[str] | None = None,
    ttl_minutes: int = APPROVAL_TTL_MINUTES,
) -> str:
    """Park a destructive call. The caller's scopes are stored with it, so approving cannot
    grant more than the request already had."""
    job_id = new_id()
    expires = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    with transaction(conn):
        conn.execute(
            "INSERT INTO jobs(id, request_id, created_at, status, capability, payload_json, "
            "title, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                job_id,
                request_id,
                now_iso(),
                AWAITING,
                capability,
                json.dumps({"args": args, "scopes": sorted(scopes or [])}),
                title,
                expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
            ),
        )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def pending_approvals(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Unexpired, undecided. An expired approval is not offered, because a tap tomorrow on
    something you asked for today is not consent."""
    return list(
        conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (AWAITING, now_iso(), limit),
        )
    )


def expired_approvals(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET status = 'expired' WHERE status = ? AND expires_at <= ?",
            (AWAITING, now_iso()),
        )
    return cur.rowcount


def decide_job(conn: sqlite3.Connection, job_id: str, status: str, by: str = "portal") -> bool:
    """Move a job out of the queue exactly once.

    The status is part of the WHERE clause on purpose: two taps on the same notification,
    or a replayed link, must not run the action twice.
    """
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = ?",
            (status, now_iso(), by, job_id, AWAITING),
        )
    return cur.rowcount > 0


def finish_job(conn: sqlite3.Connection, job_id: str, status: str, result: Any = None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET status = ?, result_json = ? WHERE id = ?",
            (status, json.dumps(result) if result is not None else None, job_id),
        )


# --- todos --------------------------------------------------------------------------
#
# A todo is a promise, not a note. Separate table so open/done/due/priority can be
# queried without scanning journal text. Soft-delete mirrors journal.

VALID_RECURRENCE = frozenset({"none", "daily", "weekly", "monthly", "yearly"})


def _todo_next_due(current: str | None, recurrence: str) -> str | None:
    """Compute next due date for a recurring todo."""
    if not current or recurrence == "none":
        return None
    try:
        # Accept ISO date or datetime
        dt = datetime.fromisoformat(current.replace("Z", "+00:00"))
    except ValueError:
        return None
    if recurrence == "daily":
        nxt = dt + timedelta(days=1)
    elif recurrence == "weekly":
        nxt = dt + timedelta(weeks=1)
    elif recurrence == "monthly":
        # Calendar month: advance month, clamp day
        year, month = dt.year, dt.month + 1
        if month > 12:
            year += 1
            month = 1
        import calendar as _cal

        day = min(dt.day, _cal.monthrange(year, month)[1])
        nxt = dt.replace(year=year, month=month, day=day)
    elif recurrence == "yearly":
        try:
            nxt = dt.replace(year=dt.year + 1)
        except ValueError:
            # Feb 29 -> Feb 28
            nxt = dt.replace(year=dt.year + 1, day=28)
    else:
        return None
    return nxt.isoformat(timespec="seconds").replace("+00:00", "Z")


def add_todo(
    conn: sqlite3.Connection,
    text: str,
    *,
    request_id: str | None = None,
    due_at: str | None = None,
    priority: int = 0,
    recurrence: str = "none",
) -> str:
    text = text.strip()
    if not text:
        raise ValueError("todo text is required")
    if priority not in (0, 1, 2):
        raise ValueError("priority must be 0, 1, or 2")
    if recurrence not in VALID_RECURRENCE:
        raise ValueError(f"recurrence must be one of {sorted(VALID_RECURRENCE)}")
    # Normalise due_at if provided
    if due_at is not None:
        due_at = due_at.strip() or None
        if due_at:
            # Validate ISO
            try:
                datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"due_at must be ISO-8601: {exc}") from exc
        else:
            due_at = None
    todo_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO todos(id, request_id, created_at, text, due_at, priority, "
            "recurrence, status) VALUES (?,?,?,?,?,?,?, 'open')",
            (todo_id, request_id, now_iso(), text, due_at, priority, recurrence),
        )
    return todo_id


def get_todo(conn: sqlite3.Connection, todo_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


def list_todos(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    include_deleted: bool = False,
    include_done: bool = True,
    limit: int = 100,
    offset: int = 0,
    query: str | None = None,
) -> list[sqlite3.Row]:
    """List todos. By default excludes soft-deleted rows, ordered open first then by due/created.

    - status: 'open'|'done'|None (None means all non-deleted)
    - query: FTS search string when provided
    """
    if query and query.strip():
        expression = to_fts_query(query)
        if expression:
            try:
                sql = (
                    "SELECT t.* FROM todos_fts f JOIN todos t ON t.rowid = f.rowid "
                    "WHERE todos_fts MATCH ? AND t.deleted_at IS NULL "
                )
                params: list[Any] = [expression]
                if status in ("open", "done"):
                    sql += "AND t.status = ? "
                    params.append(status)
                elif not include_done:
                    sql += "AND t.status = 'open' "
                sql += "ORDER BY f.rank, t.created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                return list(conn.execute(sql, params))
            except sqlite3.OperationalError:
                pass
        # Fallback to LIKE if FTS fails or query empty
        like = f"%{query.strip()}%"
        sql = "SELECT * FROM todos WHERE text LIKE ? AND deleted_at IS NULL "
        params = [like]
        if status in ("open", "done"):
            sql += "AND status = ? "
            params.append(status)
        elif not include_done:
            sql += "AND status = 'open' "
        sql += "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return list(conn.execute(sql, params))

    clauses = []
    params = []
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if status in ("open", "done"):
        clauses.append("status = ?")
        params.append(status)
    elif not include_done:
        clauses.append("status = 'open'")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Open first, then by due date (nulls last), then newest
    sql = (
        f"SELECT * FROM todos{where} "
        "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, "
        "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, created_at DESC "
        "LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return list(conn.execute(sql, params))


def count_todos(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN deleted_at IS NULL AND status='open' THEN 1 ELSE 0 END) AS open_c, "
        "SUM(CASE WHEN deleted_at IS NULL AND status='done' THEN 1 ELSE 0 END) AS done_c, "
        "SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_c, "
        "COUNT(*) AS total FROM todos"
    ).fetchone()
    return {
        "open": int(row["open_c"] or 0),
        "done": int(row["done_c"] or 0),
        "deleted": int(row["deleted_c"] or 0),
        "total": int(row["total"] or 0),
    }


def search_todos(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    return list_todos(conn, query=query, limit=limit)


def update_todo(
    conn: sqlite3.Connection,
    todo_id: str,
    *,
    text: str | None = None,
    due_at: str | None = None,
    priority: int | None = None,
    recurrence: str | None = None,
) -> bool:
    fields: list[str] = []
    params: list[Any] = []
    if text is not None:
        t = text.strip()
        if not t:
            return False
        fields.append("text = ?")
        params.append(t)
    if due_at is not None:
        # Empty string means clear
        v = due_at.strip() or None
        if v:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return False
        fields.append("due_at = ?")
        params.append(v)
    if priority is not None:
        if priority not in (0, 1, 2):
            return False
        fields.append("priority = ?")
        params.append(priority)
    if recurrence is not None:
        if recurrence not in VALID_RECURRENCE:
            return False
        fields.append("recurrence = ?")
        params.append(recurrence)
    if not fields:
        return False
    fields.append("updated_at = ?")
    params.append(now_iso())
    params.append(todo_id)
    with transaction(conn):
        cur = conn.execute(
            f"UPDATE todos SET {', '.join(fields)} WHERE id = ? AND deleted_at IS NULL",
            params,
        )
    return cur.rowcount > 0


def toggle_todo(conn: sqlite3.Connection, todo_id: str) -> sqlite3.Row | None:
    """Flip open<->done. For recurring todos, completing creates the next occurrence."""
    row = get_todo(conn, todo_id)
    if row is None or row["deleted_at"] is not None:
        return None
    now = now_iso()
    if row["status"] == "open":
        # Complete
        with transaction(conn):
            conn.execute(
                "UPDATE todos SET status='done', completed_at=?, updated_at=? WHERE id=?",
                (now, now, todo_id),
            )
            # Recurrence: spawn next open todo
            rec = row["recurrence"] or "none"
            if rec != "none":
                nxt_due = _todo_next_due(row["due_at"], rec)
                # If no due date but recurring, use now + interval
                if nxt_due is None and row["due_at"] is None:
                    base = now
                    nxt_due = _todo_next_due(base, rec)
                if nxt_due:
                    new_id_val = new_id()
                    conn.execute(
                        "INSERT INTO todos(id, request_id, created_at, text, due_at, priority, "
                        "recurrence, status) VALUES (?,?,?,?,?,?,?,'open')",
                        (
                            new_id_val,
                            row["request_id"],
                            now,
                            row["text"],
                            nxt_due,
                            row["priority"],
                            rec,
                        ),
                    )
    else:
        with transaction(conn):
            conn.execute(
                "UPDATE todos SET status='open', completed_at=NULL, updated_at=? WHERE id=?",
                (now, todo_id),
            )
    return get_todo(conn, todo_id)


def delete_todo(conn: sqlite3.Connection, todo_id: str) -> bool:
    """Soft delete."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE todos SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (now_iso(), todo_id),
        )
    return cur.rowcount > 0


def restore_todo(conn: sqlite3.Connection, todo_id: str) -> bool:
    with transaction(conn):
        cur = conn.execute("UPDATE todos SET deleted_at=NULL WHERE id=?", (todo_id,))
    return cur.rowcount > 0


def purge_todo(conn: sqlite3.Connection, todo_id: str) -> bool:
    """Hard delete, only if already soft-deleted."""
    with transaction(conn):
        cur = conn.execute("DELETE FROM todos WHERE id=? AND deleted_at IS NOT NULL", (todo_id,))
    return cur.rowcount > 0


def overdue_todos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM todos WHERE deleted_at IS NULL AND status='open' "
            "AND due_at IS NOT NULL AND due_at < ? ORDER BY due_at ASC",
            (now_iso(),),
        )
    )
