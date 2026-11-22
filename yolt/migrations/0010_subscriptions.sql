-- Channel subscriptions: follow a YouTube channel's uploads, its finished livestreams, or both.
-- Yolt checks each subscription's channel tabs every few hours (Settings) and keeps the newest long
-- videos it finds, so they can be clipped from the Subscriptions page. Nothing is clipped automatically.

-- A podcast row is a YouTube channel; subscribing creates it if no episode has yet.
CREATE TABLE subscriptions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    podcast_id bigint NOT NULL UNIQUE REFERENCES podcasts (id),
    handle text,  -- "@name", when the channel has one
    uploads boolean NOT NULL,
    streams boolean NOT NULL,  -- finished livestreams
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- The last check: when it finished and, if it failed, why (and how many times in a row). A check
    -- is due when one was asked for (a new subscription, a newly followed kind, "Check now") or the
    -- interval in Settings has passed; a failed scheduled check is retried after 1 and 5 minutes.
    checked_at timestamptz,
    check_error text,
    check_failures integer NOT NULL DEFAULT 0,
    -- Cleared by a check that started after it, so asking during a check gets another one.
    check_requested_at timestamptz DEFAULT now(),
    CHECK (uploads OR streams)
);

-- Videos the checks found. They stay after an unsubscribe, like episodes do.
CREATE TABLE channel_videos (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform text NOT NULL,
    video_id text NOT NULL,
    podcast_id bigint NOT NULL REFERENCES podcasts (id),
    kind text NOT NULL CHECK (kind IN ('upload', 'stream')),
    title text NOT NULL,
    duration_ms bigint CHECK (duration_ms > 0),
    -- As precise as the channel page says it ("3 days ago"); an episode gets the exact time once clipped.
    published_at timestamptz,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, video_id)
);
CREATE INDEX channel_videos_published_idx ON channel_videos (published_at DESC NULLS LAST, id DESC);
CREATE INDEX channel_videos_podcast_idx ON channel_videos (podcast_id);
