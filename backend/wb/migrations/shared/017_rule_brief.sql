-- 开盘前简报 (spec §7.1, 1.34): how a rule's results go into the briefing — list (its stocks, the first few), count
-- (how many), off; NULL: by what the rule is (one with a structure stage lists, a formula-only screener counts).
ALTER TABLE rule_state ADD COLUMN brief TEXT;
