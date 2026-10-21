-- Per-profile application state (spec §4.3).
CREATE TABLE snapshot (
  id         TEXT PRIMARY KEY,
  market     TEXT NOT NULL,
  as_of      TEXT NOT NULL,          -- last trading date contained, YYYY-MM-DD
  created_at TEXT NOT NULL,
  parent_id  TEXT,
  manifest   TEXT NOT NULL,          -- JSON {dataset: {partition: relative file path}}
  note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX snapshot_by_market ON snapshot(market, created_at);

CREATE TABLE quality_issue (
  snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
  dataset     TEXT NOT NULL,
  check_name  TEXT NOT NULL,
  severity    TEXT NOT NULL,         -- row | warn  (block-level issues never get published)
  listing_id  TEXT,
  date        TEXT,
  detail      TEXT NOT NULL
);
CREATE INDEX quality_by_snapshot ON quality_issue(snapshot_id);

CREATE TABLE capability (
  provider      TEXT NOT NULL,
  market        TEXT NOT NULL,
  dataset       TEXT NOT NULL,
  available     INTEGER NOT NULL,
  history_start TEXT,
  latest_date   TEXT,
  rate_per_min  INTEGER,
  detail        TEXT NOT NULL DEFAULT '',
  probed_at     TEXT NOT NULL,
  PRIMARY KEY (provider, market, dataset)
);

-- One row per fetched unit (a date, a ticker or "all") that is safely in staging.
CREATE TABLE fetch_log (
  market     TEXT NOT NULL,
  provider   TEXT NOT NULL,
  dataset    TEXT NOT NULL,
  unit       TEXT NOT NULL,
  rows       INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (market, provider, dataset, unit)
);
