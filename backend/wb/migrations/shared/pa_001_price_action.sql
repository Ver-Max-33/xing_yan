-- 价格行为分析 (dev 分支，design 2026-10-20 §5、§8)：每次分析一行，存交给 AI 的 100 根日线、两步的结果、AI 原方案、
-- 程序改后的方案和改动清单；两步对话的请求与回答在 llm_call 里，这里只存编号。之后按 A 股规则对账（pa_trade），
-- 并算第 5、20 个交易日的超额。只在自己的表里，主线的表一张不动：主线的程序打开同一个库照常能用。
CREATE TABLE pa_analysis (
  id           TEXT PRIMARY KEY,
  listing_id   TEXT NOT NULL,
  day          TEXT NOT NULL,                 -- 第 0 天：交给 AI 的最后一根日线的日子
  profile      TEXT NOT NULL,                 -- 测试档的不对账（用的是不复权价）
  trigger      TEXT NOT NULL DEFAULT 'manual',
  demo         INTEGER NOT NULL DEFAULT 0,    -- 演示数据上做的，不对账
  adjusted     INTEGER NOT NULL DEFAULT 1,    -- 前复权价（正式档）
  intraday     INTEGER NOT NULL DEFAULT 0,    -- 盘中分析：第 0 天比分析时已开盘的交易日早
  first_of_day INTEGER NOT NULL DEFAULT 1,    -- 同一只股票、同一个第 0 天、同一档的第一次成功分析：只有它进评估
  status       TEXT NOT NULL,                 -- running / ok / failed / cancelled
  stage        TEXT,                          -- 做到哪一步
  error        TEXT,
  model        TEXT,
  cost         REAL,                          -- 元，到结束为止花的
  latency_ms   INTEGER,
  call_ids     TEXT NOT NULL DEFAULT '[]',    -- JSON：llm_call 的编号
  prompt_hash  TEXT NOT NULL,                 -- 全部提示词文字和 A 股改动的指纹
  input        TEXT NOT NULL,                 -- JSON：100 根日线（价格就是表里那样）、K1 收盘、下一个交易日的涨跌停价
  stage1       TEXT,                          -- JSON
  stage2       TEXT,                          -- JSON
  ai_plan      TEXT,                          -- JSON：AI 原方案
  final_plan   TEXT,                          -- JSON：程序改后的方案
  changes      TEXT,                          -- JSON：[{field, before, after, reason}]
  previous_id  TEXT,                          -- 交给它的"上一次的方案"出自哪次分析
  created_at   TEXT NOT NULL,                 -- 带时区：市场自己的日期决定第一个开盘
  finished_at  TEXT,
  start_date   TEXT,                          -- 方案生效的第一个开盘所在的交易日；日历还没有那天时为空，之后补上
  fwd_outcome  TEXT,                          -- settled / gone / halt
  r5           REAL,
  x5           REAL,
  r20          REAL,
  x20          REAL,
  settled      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX pa_analysis_by_listing ON pa_analysis (listing_id, created_at);

-- 每次分析 × 两份方案（ai / final）× 两个止盈（tp1 / tp2）一行，以及它的随机对照
CREATE TABLE pa_trade (
  analysis_id     TEXT NOT NULL,
  track           TEXT NOT NULL,              -- ai / final
  target          TEXT NOT NULL,              -- tp1 / tp2
  status          TEXT NOT NULL,              -- no_plan / short / waiting / missed / open / closed / gone
  entry_date      TEXT,
  entry_price     REAL,
  exit_date       TEXT,
  exit_price      REAL,
  reason          TEXT,                       -- stop / tp1 / tp2 / time / gone
  days            INTEGER,
  ret             REAL,                       -- 扣费后
  r               REAL,                       -- 赚的钱 ÷ 买入价到止损的距离
  ctrl_n          INTEGER,
  ctrl_bought     INTEGER,
  ctrl_win        REAL,
  ctrl_ret_mean   REAL,
  ctrl_ret_median REAL,
  ctrl_r_mean     REAL,
  final           INTEGER NOT NULL DEFAULT 0, -- 1：结果不会再变
  PRIMARY KEY (analysis_id, track, target)
);
