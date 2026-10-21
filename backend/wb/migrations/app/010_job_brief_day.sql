-- 开盘前简报 (spec §7.1, 1.34): the market-local day whose briefing was made (once a day, a restart too).
ALTER TABLE job_state ADD COLUMN brief_day TEXT;
