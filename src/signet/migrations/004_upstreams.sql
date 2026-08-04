-- Remote MCP servers mounted as internal capabilities.
--
-- This is the routing station made real: point signet at any MCP server and its tools become
-- things the agent can use, without writing a capability. Credentials stay here rather than
-- being typed into a phone, and the ring's tool list is untouched because everything mounts
-- internally, reachable only through the four verbs.
CREATE TABLE upstreams (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,   -- namespace, so "home" gives home.turn_on_light
    url           TEXT NOT NULL,
    auth_header   TEXT,                   -- used verbatim, e.g. "Bearer abc"
    enabled       INTEGER NOT NULL DEFAULT 1,
    trusted       INTEGER NOT NULL DEFAULT 0,  -- 0 means results are treated as untrusted
    approve_all   INTEGER NOT NULL DEFAULT 0,  -- 1 means every tool needs approval
    allow_tools   TEXT,                   -- JSON array; null means all of them
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    last_error    TEXT,
    tools_json    TEXT                    -- cached list, so a boot needs no network
);
