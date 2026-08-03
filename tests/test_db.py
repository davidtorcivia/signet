"""Schema, migrations, and the queries the feed and router depend on."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from signet import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "signet.db")
    db.migrate(connection)
    return connection


def test_migrations_are_idempotent(tmp_path: Path):
    connection = db.connect(tmp_path / "signet.db")
    first = db.migrate(connection)
    assert first, "expected at least one migration to run"
    assert db.migrate(connection) == [], "second boot must apply nothing"


def test_tokens_are_stored_hashed(conn: sqlite3.Connection):
    token_id, plaintext = db.create_token(conn, "ring", ["journal:write"])
    row = conn.execute("SELECT token_hash FROM tokens WHERE id = ?", (token_id,)).fetchone()
    assert plaintext not in row["token_hash"]
    assert db.lookup_token(conn, plaintext)["id"] == token_id


def test_revoked_token_stops_resolving(conn: sqlite3.Connection):
    token_id, plaintext = db.create_token(conn, "old", [])
    db.revoke_token(conn, token_id)
    assert db.lookup_token(conn, plaintext) is None


def test_seed_token_is_idempotent(conn: sqlite3.Connection):
    token = "y" * 48
    assert db.seed_token(conn, token) == db.seed_token(conn, token)


def test_journal_is_full_text_searchable(conn: sqlite3.Connection):
    db.add_journal(conn, "the darkroom timer needs a new bulb")
    db.add_journal(conn, "buy more fixer")
    assert [r["text"] for r in db.search_journal(conn, "bulb")] == [
        "the darkroom timer needs a new bulb"
    ]
    assert db.search_journal(conn, "nothingmatches") == []


def test_search_survives_speech_punctuation(conn: sqlite3.Connection):
    """Queries arrive from a transcript, so they carry punctuation that is not valid FTS
    syntax. That must degrade to a LIKE scan, not raise."""
    db.add_journal(conn, "call the lab about the timer")
    assert [r["text"] for r in db.search_journal(conn, "timer?!")] == [
        "call the lab about the timer"
    ]


def test_deleting_a_journal_row_updates_the_index(conn: sqlite3.Connection):
    entry_id = db.add_journal(conn, "temporary thought")
    conn.execute("DELETE FROM journal WHERE id = ?", (entry_id,))
    assert db.search_journal(conn, "temporary") == []


def test_request_lifecycle_records_cost_and_latency(conn: sqlite3.Connection):
    request_id = db.start_request(conn, text="what did I say", source="mcp:ring", verb="ask")
    db.finish_request(
        conn, request_id, status="ok", result={"answer": "yes"}, latency_ms=42, cost_usd=0.0007
    )
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["result_json"]) == {"answer": "yes"}
    assert row["latency_ms"] == 42
    assert db.spend_today(conn) == pytest.approx(0.0007)


def test_kill_switch_round_trips(conn: sqlite3.Connection):
    assert db.kill_switch_on(conn) is False
    db.set_setting(conn, "kill_switch", "on")
    assert db.kill_switch_on(conn) is True


def test_legacy_journal_is_imported_once_and_renamed(conn: sqlite3.Connection, tmp_path: Path):
    """P0 captures are real notes. They get imported, not stranded, and the file is renamed so
    only one write path stays live."""
    jsonl = tmp_path / "journal.jsonl"
    jsonl.write_text(
        json.dumps({"id": "abc", "received_at": "2026-08-01T10:00:00Z", "text": "old note"}) + "\n",
        encoding="utf-8",
    )

    assert db.import_legacy_journal(conn, jsonl) == 1
    assert not jsonl.exists()
    assert (tmp_path / "journal.jsonl.imported").exists()
    assert [r["text"] for r in db.search_journal(conn, "old")] == ["old note"]

    # A second call is a no-op because the file is gone.
    assert db.import_legacy_journal(conn, jsonl) == 0
