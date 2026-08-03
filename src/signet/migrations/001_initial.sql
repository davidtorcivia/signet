-- Tokens. Stored hashed: these are 48-byte random secrets, not passwords, so SHA-256 with no
-- KDF is right. The plaintext is shown once at creation and never again.
CREATE TABLE tokens (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL,
    token_hash         TEXT    NOT NULL UNIQUE,
    scopes             TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    created_at         TEXT    NOT NULL,
    last_seen_at       TEXT,
    revoked_at         TEXT,
    rate_limit_per_min INTEGER NOT NULL DEFAULT 60
);

-- Every request end to end. This table is the feed, the audit log, and the eval set.
CREATE TABLE requests (
    id          TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    source      TEXT NOT NULL,               -- mcp:ring | webhook | cron | web | api
    verb        TEXT,                        -- capture | ask | schedule | do
    client_id   INTEGER REFERENCES tokens(id),
    text        TEXT NOT NULL,
    plan_json   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|ok|error|queued|denied
    result_json TEXT,
    error       TEXT,
    latency_ms  INTEGER,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER
);
CREATE INDEX idx_requests_received ON requests(received_at DESC);
CREATE INDEX idx_requests_status   ON requests(status);

-- The journal is the corpus. Everything the ring captures lands here.
CREATE TABLE journal (
    id         TEXT PRIMARY KEY,
    request_id TEXT REFERENCES requests(id),
    created_at TEXT NOT NULL,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note'
);
CREATE INDEX idx_journal_created ON journal(created_at DESC);

-- External-content FTS5: the index points at journal rather than duplicating the text.
CREATE VIRTUAL TABLE journal_fts USING fts5(
    text,
    content='journal',
    content_rowid='rowid'
);

CREATE TRIGGER journal_ai AFTER INSERT ON journal BEGIN
    INSERT INTO journal_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER journal_ad AFTER DELETE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER journal_au AFTER UPDATE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO journal_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- Async work (P2). Created now so the schema does not need a migration to start using it.
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    request_id  TEXT REFERENCES requests(id),
    created_at  TEXT NOT NULL,
    run_after   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|awaiting_approval
    capability  TEXT,
    payload_json TEXT,
    result_json TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_jobs_status ON jobs(status, run_after);

-- Single-row settings. The kill switch lives here so it survives a restart.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO settings(key, value) VALUES ('kill_switch', 'off');
