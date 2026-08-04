-- The approval queue.
--
-- The ring cannot answer a confirmation prompt: no session, no elicitation, and it is often
-- in a pocket. So anything destructive does not run when asked. It queues, the user is told,
-- and it executes on one deliberate tap afterwards.
ALTER TABLE jobs ADD COLUMN title TEXT;
ALTER TABLE jobs ADD COLUMN expires_at TEXT;
ALTER TABLE jobs ADD COLUMN decided_at TEXT;
ALTER TABLE jobs ADD COLUMN decided_by TEXT;

CREATE INDEX idx_jobs_pending ON jobs(status, created_at DESC);
