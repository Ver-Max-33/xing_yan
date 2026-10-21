-- 观察条目 (spec §11): a stock you watch — its status, where it came from and at what close; the texts (why, for,
-- against, notes) with every version kept; fail lines and review dates; and the record of what happened.
CREATE TABLE watch_entry (
  listing_id  TEXT PRIMARY KEY,
  status      TEXT NOT NULL,          -- research 待研究 / watching 观察中 / passed 暂不考虑 / closed 已关闭
  source      TEXT NOT NULL DEFAULT '',
  added_at    TEXT NOT NULL,
  added_day   TEXT,                   -- the trading day of the close it was added at
  added_price REAL,                   -- that close, the exchange's price
  updated_at  TEXT NOT NULL
);
CREATE TABLE watch_text (             -- every version; the newest of each kind is the current text
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT NOT NULL,
  kind       TEXT NOT NULL,           -- reason / for / against / note
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX watch_text_by_listing ON watch_text (listing_id, kind, id);
CREATE TABLE watch_condition (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- fail: a close crossing a price / review: a day to look again / manual
  text        TEXT NOT NULL,
  op          TEXT,                   -- < or > (fail)
  price       REAL,                   -- the exchange's price (fail)
  due         TEXT,                   -- YYYY-MM-DD (review)
  created_at  TEXT NOT NULL,
  fired_day   TEXT,                   -- the day it fired (once)
  fired_value REAL
);
CREATE TABLE watch_record (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT NOT NULL,
  text       TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE INDEX watch_record_by_listing ON watch_record (listing_id, id);
-- the stocks already in a group are watched from the day they were first added (their close that day)
INSERT INTO watch_entry (listing_id, status, source, added_at, added_day, added_price, updated_at)
  SELECT listing_id, 'watching', '自选分组', MIN(added_at), substr(MIN(added_at), 1, 10), NULL, MIN(added_at)
  FROM watch_item GROUP BY listing_id;
INSERT INTO watch_record (listing_id, text, at) SELECT listing_id, '加入观察：自选分组', added_at FROM watch_entry;
