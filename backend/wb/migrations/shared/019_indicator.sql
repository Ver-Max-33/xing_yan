-- 指标库 (design 2026-10-10): your indicators, as rules are kept — each save of a changed text a new version, never
-- changed after; the built-in ones ship with the program. `archived`: put away (left out of the K 线 menu).
CREATE TABLE indicator_version (
  indicator_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  hash TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (indicator_id, version)
);
CREATE TABLE indicator_state (
  indicator_id TEXT PRIMARY KEY,
  archived INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
