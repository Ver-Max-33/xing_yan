-- 个股研究 (spec §10.3) and its forward evaluation (§10.4): the evidence pack as it was sent, the model's checked answer
-- and why it needs a second look; 20 trading days on, what the stock did against the market's benchmark from the first
-- open after the research was made. Forward only: a research is never run on the past. Shared by the data profiles,
-- each record read and settled on the profile it was made on; one made on the demo data is never evaluated.
CREATE TABLE research (
  id          TEXT PRIMARY KEY,
  listing_id  TEXT NOT NULL,
  day         TEXT NOT NULL,          -- the day the evidence is as of
  profile     TEXT NOT NULL,
  demo        INTEGER NOT NULL DEFAULT 0,
  trigger     TEXT NOT NULL,          -- manual / daily
  pack        TEXT NOT NULL,          -- JSON
  pack_hash   TEXT NOT NULL,
  output      TEXT NOT NULL,          -- JSON
  status      TEXT NOT NULL,          -- ok / 需复核
  flags       TEXT NOT NULL,          -- JSON [why it needs a second look]
  call_id     TEXT,
  model       TEXT,
  cost        REAL,
  created_at  TEXT NOT NULL,          -- with its UTC offset: the market's own date decides the first open after it
  entry_date  TEXT,                   -- settled: that first open's day
  settled_at  TEXT,                   -- the 20th trading day from it
  outcome     TEXT,                   -- settled / gone (stopped trading) / halt (no trade that first day)
  return20    REAL,
  excess20    REAL
);
CREATE INDEX research_by_listing ON research (listing_id, created_at);
