-- Datasets that came later than the day's bars (spec §4.5 按数据集分别判定就绪): JSON {"day", "datasets", "next"} —
-- asked again every poll until the ready window ends; the rules that read them wait.
ALTER TABLE job_state ADD COLUMN pending TEXT;
