-- Todos: the actionable subset of captures.
--
-- Separate from journal so they can be completed, filtered, and counted without
-- mixing states. A journal note is a fact; a todo is a promise with a lifecycle.
-- Completed todos stay visible (done) rather than vanishing, so "what did I do
-- this week" is answerable.
CREATE TABLE todos (
    id           TEXT PRIMARY KEY,
    request_id   TEXT REFERENCES requests(id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT,
    completed_at TEXT,
    due_at       TEXT,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
    priority     INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 2),
    recurrence   TEXT NOT NULL DEFAULT 'none' CHECK (recurrence IN ('none','daily','weekly','monthly','yearly')),
    deleted_at   TEXT
);
CREATE INDEX idx_todos_status ON todos(status, created_at DESC);
CREATE INDEX idx_todos_due ON todos(due_at) WHERE due_at IS NOT NULL;
CREATE INDEX idx_todos_live ON todos(deleted_at, status, created_at DESC);

-- External-content FTS5 for todos, mirroring journal_fts.
CREATE VIRTUAL TABLE todos_fts USING fts5(
    text,
    content='todos',
    content_rowid='rowid'
);

CREATE TRIGGER todos_ai AFTER INSERT ON todos BEGIN
    INSERT INTO todos_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER todos_ad AFTER DELETE ON todos BEGIN
    INSERT INTO todos_fts(todos_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER todos_au AFTER UPDATE ON todos BEGIN
    INSERT INTO todos_fts(todos_fts, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO todos_fts(rowid, text) VALUES (new.rowid, new.text);
END;
