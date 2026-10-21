-- One row per rule run (spec §5.9, §7.1): which rule version ran on which snapshot, pool and day(s);
-- the per-listing results are a Parquet file runs/{id}.parquet in the profile (kept when gc drops
-- the snapshot's caches).
CREATE TABLE rule_run (
  id          TEXT PRIMARY KEY,
  rule_id     TEXT NOT NULL,
  version     INTEGER NOT NULL,
  rule_hash   TEXT NOT NULL,
  market      TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  mode        TEXT NOT NULL,          -- date | range
  day         TEXT,                   -- date mode
  start       TEXT,                   -- range mode
  end         TEXT,
  pool        TEXT NOT NULL,          -- JSON: null for the whole market, else the listing ids
  counts      TEXT NOT NULL,          -- JSON {"signal": n, "watch": n, "unknown": n, "fail": n}
  created_at  TEXT NOT NULL
);
CREATE INDEX rule_run_by_rule ON rule_run(rule_id, created_at);
