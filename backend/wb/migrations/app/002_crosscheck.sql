-- Shadow verification against an independent source (spec §4.6). Never used to change data.
CREATE TABLE crosscheck (
  snapshot_id TEXT NOT NULL,
  reference   TEXT NOT NULL,          -- verifier name, e.g. sse_szse, yfinance, jquants
  listing_id  TEXT NOT NULL,
  date        TEXT NOT NULL,
  field       TEXT NOT NULL,
  stored      REAL,
  ref_value   REAL,
  ok          INTEGER NOT NULL,
  checked_at  TEXT NOT NULL
);
CREATE INDEX crosscheck_by_snapshot ON crosscheck(snapshot_id);
