-- How many times Yolt stopped unexpectedly (a crash, a kill, the machine running out of memory) while
-- working on a run, since it was last queued by Retry. A clean shutdown puts its run back in the queue itself
-- and doesn't count. A run that keeps taking the server down with it fails after MAX_INTERRUPTIONS instead of
-- being retried on every start; Retry resets the count.

ALTER TABLE runs ADD COLUMN interruptions integer NOT NULL DEFAULT 0;
