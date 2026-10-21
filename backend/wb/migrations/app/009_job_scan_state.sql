-- A scan's own progress, kept as it goes (1.32: pause and continue): the snapshot it scans, how many rules, and the
-- rules it has finished with their runs — paused, or cut off by quitting the app, it goes on from the next rule.
ALTER TABLE job_state ADD COLUMN scan_state TEXT;
