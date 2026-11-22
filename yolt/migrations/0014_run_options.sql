-- A run's own clip options (POST /runs `options`: min_clip_seconds, max_clip_seconds, max_clips,
-- candidates_per_window), each overriding its setting for that run; '{}' follows Settings. A video is clipped
-- once per focus and options: the same video, instructions and options return the existing run.
ALTER TABLE runs ADD COLUMN options jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(options) = 'object');
ALTER TABLE runs DROP CONSTRAINT runs_episode_id_instructions_key;
ALTER TABLE runs ADD CONSTRAINT runs_episode_id_instructions_options_key UNIQUE (episode_id, instructions, options);
