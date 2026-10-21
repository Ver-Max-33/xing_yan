-- 最近一次错误 (spec §4.9): per data source and dataset, the last fetch that failed (what was asked, why, when) —
-- listed on the settings page next to what the probe found.
CREATE TABLE fetch_error (
  market   TEXT NOT NULL,
  provider TEXT NOT NULL,
  dataset  TEXT NOT NULL,
  unit     TEXT NOT NULL,
  error    TEXT NOT NULL,
  at       TEXT NOT NULL,
  PRIMARY KEY (market, provider, dataset)
);
