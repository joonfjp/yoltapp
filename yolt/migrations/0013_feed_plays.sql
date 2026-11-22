-- The feed (the TV page) plays clips one after another. Each clip's plays there are counted so the feed
-- favours clips it hasn't shown yet: one row per clip, updated in place, not one per play.

CREATE TABLE feed_plays (
    clip_id bigint PRIMARY KEY REFERENCES clips (id),
    play_count integer NOT NULL CHECK (play_count > 0),
    last_played_at timestamptz NOT NULL DEFAULT now()
);
