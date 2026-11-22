-- Speaker inference retries rotate through episodes instead of retrying the same one, and an episode
-- whose answers keep coming back unusable is given up on (its speakers can still be set by hand).
-- Outages of the LLM itself don't count as attempts.

ALTER TABLE episodes ADD COLUMN speakers_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE episodes ADD COLUMN speakers_tried_at timestamptz;
