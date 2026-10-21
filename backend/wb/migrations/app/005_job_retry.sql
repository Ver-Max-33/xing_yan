-- A failed update or re-fetch is tried again after a while (local time, no zone), not on every tick of the scheduler.
ALTER TABLE job_state ADD COLUMN retry_after TEXT;
