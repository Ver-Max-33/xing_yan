-- Workspace (spec §11): watchlists, drawings, recently viewed. Shared by both profiles.
CREATE TABLE watch_group (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  sort       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE watch_item (
  group_id   INTEGER NOT NULL REFERENCES watch_group(id) ON DELETE CASCADE,
  listing_id TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  added_at   TEXT NOT NULL,
  PRIMARY KEY (group_id, listing_id)
);
-- Anchors are (date, raw price) so a later ex-rights event never moves a line off its bars (spec §8.2).
CREATE TABLE drawing (
  id         TEXT PRIMARY KEY,
  listing_id TEXT NOT NULL,
  kind       TEXT NOT NULL,
  points     TEXT NOT NULL,          -- JSON [{"date": "YYYY-MM-DD", "price": raw}]
  styles     TEXT NOT NULL DEFAULT '{}',
  alert      INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX drawing_by_listing ON drawing(listing_id);
CREATE TABLE recent (
  listing_id TEXT PRIMARY KEY,
  viewed_at  TEXT NOT NULL
);
INSERT INTO watch_group (name, sort, created_at) VALUES ('自选', 0, datetime('now'));
INSERT INTO watch_group (name, sort, created_at) VALUES ('养箱', 1, datetime('now'));
