-- A video can be clipped any number of times, with the same focus and clip options or not: each POST /runs
-- queues a new run (reusing the video's transcript). An episode's runs are still found by it.
ALTER TABLE runs DROP CONSTRAINT runs_episode_id_instructions_options_key;
CREATE INDEX runs_episode_idx ON runs (episode_id, id);
