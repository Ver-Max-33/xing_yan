-- Notifications on their way out (spec §7.1): one row per notice, keyed by what it is about — (stock, rule version,
-- structure, event, known_at), or (event, market, day) for one without a structure — so a notice is sent at least
-- once and never twice, also across a restart. In shared.sqlite: there is one bot and one rate limit, whatever
-- data profile a market uses.
CREATE TABLE outbox (
  id          INTEGER PRIMARY KEY,
  idem        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL,
  market      TEXT,
  day         TEXT,
  payload     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  next_try_at TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  sent_at     TEXT,
  last_error  TEXT
);
CREATE INDEX outbox_pending ON outbox (sent_at, next_try_at);
