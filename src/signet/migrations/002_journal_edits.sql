-- Editing and deleting journal entries.
--
-- Delete is soft. The journal is the corpus `ask` answers from, so a wrong entry has to be
-- removable, but this project's whole premise is that what you said is never lost. A row with
-- deleted_at set is invisible to search and to the model, and still recoverable.
ALTER TABLE journal ADD COLUMN updated_at TEXT;
ALTER TABLE journal ADD COLUMN deleted_at TEXT;

CREATE INDEX idx_journal_live ON journal(deleted_at, created_at DESC);
