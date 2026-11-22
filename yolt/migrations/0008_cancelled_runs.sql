-- A queued or processing run can be cancelled. It keeps the stage it stopped at, like a failed run,
-- and retrying it (or submitting the same video and instructions again) queues it again.

ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'cancelled'));
