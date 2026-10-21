-- 稳健性's heatmap axes as a run was asked for (1.32: a paused or cut-off run is continued from what it kept, so
-- everything that made it is stored with it). JSON list of parameter ids, or NULL for the rule's own choice.
ALTER TABLE validation_run ADD COLUMN axes TEXT;
