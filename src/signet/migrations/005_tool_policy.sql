-- Per-tool policy for a mounted server.
--
-- The name heuristic decides whether a tool looks like a read, and it cannot do better than
-- that: turn_on_light and unlock_door are indistinguishable to a schema. Being asked to
-- approve a light is friction with no safety in it, so the guess is only a default and this
-- is the override.
--
-- JSON object of tool name to one of: "auto" (run directly), "approve" (always ask),
-- "off" (do not mount at all). Anything absent falls back to the heuristic.
ALTER TABLE upstreams ADD COLUMN tool_policy TEXT;
