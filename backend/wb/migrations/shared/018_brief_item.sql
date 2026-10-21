-- 开盘前简报 (spec §7.1, 2026-10-10): what it lists — the 选股 views you put into it, in your order: a rule, its 附加条件
-- (formulas, a JSON list, all held on the scan's day) and maybe 同时满足另一条规则 (its 命中 / 命中或潜伏), each listed (its
-- stocks, the first few) or counted. It replaces each rule's own brief mode (rule_state.brief: kept, no longer read).
CREATE TABLE brief_item (
  id INTEGER PRIMARY KEY,
  rule_id TEXT NOT NULL,
  wheres TEXT NOT NULL DEFAULT '[]',
  also_rule TEXT,
  also_status TEXT,
  mode TEXT NOT NULL DEFAULT 'list',
  sort INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
