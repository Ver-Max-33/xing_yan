-- 组合回测 (spec §9.2): each market's book a run was asked for — cash, positions, exits, costs (JSON by market). Its
-- trades, cash ledger and NAV are Parquet files under <data root>/validation/<id>.<trades|ledger|nav>.parquet.
ALTER TABLE validation_run ADD COLUMN settings TEXT;
