-- Where each market's daily cycle stands (spec §4.5, §7.1), so a restart picks up where it left off: the market
-- day whose update ran, the trading day last re-fetched, the snapshot last scanned and what that scan found.
-- Per data profile (app.sqlite): switching a market to the other profile starts that profile's own cycle.
CREATE TABLE job_state (
  market           TEXT PRIMARY KEY,
  status           TEXT NOT NULL,       -- idle / updating / scanning / done / delayed / error
  message          TEXT,
  update_day       TEXT,
  refetch_day      TEXT,
  scanned_snapshot TEXT,
  scan_at          TEXT,
  scan_summary     TEXT,                -- JSON: per rule, its counts and how many changed
  updated_at       TEXT NOT NULL
);
