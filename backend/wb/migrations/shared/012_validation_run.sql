-- 验证的运行 (spec §9): what was run on what — the rule version or the formula, the markets and their data profiles,
-- the range and the 样本内 / 样本外 split fixed before it ran — and what it found (JSON; each signal's outcome is a
-- Parquet file under <data root>/validation/<id>.parquet). Kept: results and suggestions stay on record (§9.5).
CREATE TABLE validation_run (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,          -- event / checkup / single
  rule_id     TEXT,
  version     INTEGER,
  rule_hash   TEXT,
  expr        TEXT,                   -- single: the formula tested on its own
  markets     TEXT NOT NULL,          -- JSON ["CN", "JP"]
  profiles    TEXT NOT NULL,          -- JSON {"CN": "test", ...}
  start       TEXT NOT NULL,
  end         TEXT NOT NULL,
  oos_start   TEXT NOT NULL,
  status      TEXT NOT NULL,          -- queued / running / done / error
  progress    TEXT,
  error       TEXT,
  result      TEXT,
  created_at  TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX validation_run_by_rule ON validation_run (rule_id, created_at);
