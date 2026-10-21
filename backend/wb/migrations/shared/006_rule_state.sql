-- Per-rule settings beside its versions (spec §7): an enabled rule is scanned after every close (P4) and its
-- watch list monitored during the session (P5); an archived one is put away from the library list.
-- Built-in rules have rows here too (they can be enabled or put away like any other).
CREATE TABLE rule_state (
  rule_id    TEXT PRIMARY KEY,
  enabled    INTEGER NOT NULL DEFAULT 0,
  archived   INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
