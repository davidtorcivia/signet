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


def test_spoken_questions_match_their_notes(conn: sqlite3.Connection):
    """Regression, and an expensive one.

    A spoken question is mostly scaffolding. Requiring a note to contain "what", "happened"
    and "to" meant natural questions matched nothing, and `ask` reads an empty journal result
    as grounds to run a paid web search. So this failing silently cost money on questions the
    journal could answer.
    """
    db.add_journal(conn, "the darkroom enlarger bulb blew on Tuesday and needs replacing")

    for question in (
        "what happened to the enlarger?",
        "when did the enlarger bulb blow",
        "tell me about the darkroom",
        "did I say anything about replacing the bulb",
    ):
        found = db.search_journal(conn, question)
        assert found, f"{question!r} found nothing"
        assert "enlarger" in found[0]["text"]


def test_best_match_ranks_first(conn: sqlite3.Connection):
    db.add_journal(conn, "bought a new lens cap")
    db.add_journal(conn, "the enlarger lamp needs a new bulb")
    db.add_journal(conn, "lens cleaning cloth is in the drawer")

    found = db.search_journal(conn, "what about the enlarger bulb")
    assert "enlarger" in found[0]["text"], [r["text"] for r in found]


def test_a_question_of_only_stopwords_still_searches(conn: sqlite3.Connection):
    db.add_journal(conn, "what did I do")
    assert db.search_journal(conn, "what did I do")


def test_unrelated_question_still_finds_nothing(conn: sqlite3.Connection):
    """OR plus ranking must not turn every query into a match on everything."""
    db.add_journal(conn, "the enlarger bulb blew")
    assert db.search_journal(conn, "quantum chromodynamics") == []


def test_editing_a_note_updates_what_search_finds(conn: sqlite3.Connection):
    """The journal is what `ask` answers from, so a corrected note must be findable by its new
    wording and no longer by the old."""
    entry_id = db.add_journal(conn, "the enlarger bulb blew")
    assert db.update_journal(conn, entry_id, "the enlarger fuse blew, not the bulb")

    assert db.search_journal(conn, "fuse")
    assert db.search_journal(conn, "bulb")[0]["text"].endswith("not the bulb")
    assert db.get_journal(conn, entry_id)["updated_at"]


def test_empty_edit_is_refused(conn: sqlite3.Connection):
    entry_id = db.add_journal(conn, "keep me")
    assert db.update_journal(conn, entry_id, "   ") is False
    assert db.get_journal(conn, entry_id)["text"] == "keep me"


def test_deleted_notes_leave_the_corpus(conn: sqlite3.Connection):
    """A deleted note must not come back as an answer. That is the whole point of removing
    it, given the journal feeds the model."""
    entry_id = db.add_journal(conn, "the darkroom is booked Thursday")
    assert db.search_journal(conn, "darkroom")

    assert db.delete_journal(conn, entry_id)
    assert db.search_journal(conn, "darkroom") == []
    assert db.recent_journal(conn) == []
    assert db.list_journal(conn) == []


def test_delete_is_recoverable(conn: sqlite3.Connection):
    """Soft, because this project's premise is that what you said is not lost. A mis-tap on a
    phone should cost one tap back."""
    entry_id = db.add_journal(conn, "recover me")
    db.delete_journal(conn, entry_id)

    assert [r["text"] for r in db.list_journal(conn, deleted=True)] == ["recover me"]
    assert db.restore_journal(conn, entry_id)
    assert db.search_journal(conn, "recover")


def test_deleting_twice_is_harmless(conn: sqlite3.Connection):
    entry_id = db.add_journal(conn, "once")
    assert db.delete_journal(conn, entry_id) is True
    assert db.delete_journal(conn, entry_id) is False


def test_editing_a_deleted_note_does_nothing(conn: sqlite3.Connection):
    entry_id = db.add_journal(conn, "gone")
    db.delete_journal(conn, entry_id)
    assert db.update_journal(conn, entry_id, "back?") is False


def test_purge_only_applies_to_deleted_notes(conn: sqlite3.Connection):
    """Permanent deletion must not be reachable for a live note."""
    entry_id = db.add_journal(conn, "still here")
    assert db.purge_journal(conn, entry_id) is False
    assert db.get_journal(conn, entry_id)

    db.delete_journal(conn, entry_id)
    assert db.purge_journal(conn, entry_id) is True
    assert db.get_journal(conn, entry_id) is None
