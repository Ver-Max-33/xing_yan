-- 盘中提醒 (spec §7.2, §8.5 预警中心): each intraday alert of the day — a rule's next stage met on today's bar (kind rule,
-- key = the rule), a price crossing a line you drew (line, key = the drawing) or a 潜伏 box's top (box, key = the rule) —
-- with what was seen; a rule alert waits for the close (pending → confirmed / revoked), a price alert needs none.
CREATE TABLE live_alert (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  market TEXT NOT NULL,
  listing_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  at TEXT NOT NULL,
  price REAL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  detail TEXT,
  status TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX live_alert_day ON live_alert (day, market);
-- 今天不再提醒: a stock muted for the rest of its day
CREATE TABLE live_mute (day TEXT NOT NULL, listing_id TEXT NOT NULL, PRIMARY KEY (day, listing_id));
