-- Yolt's catalogue: videos, transcripts, clipping runs, and the clips they produce.
--
-- An episode is a video. A run is one clipping job on it (a video + instructions);
-- re-clipping with new instructions makes a new run and reuses the transcript.
-- Clips are immutable once their run is ready: compilations will reference them.
-- Times are integer milliseconds in the original video; word ranges are inclusive.

CREATE TABLE settings (
    key text PRIMARY KEY,
    value text NOT NULL
);

-- A YouTube channel.
CREATE TABLE podcasts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform text NOT NULL,
    external_id text NOT NULL,  -- channel id, else channel URL, else name
    name text NOT NULL,
    url text,
    UNIQUE (platform, external_id)
);

CREATE TABLE episodes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform text NOT NULL,
    video_id text NOT NULL,
    source_url text NOT NULL,
    podcast_id bigint REFERENCES podcasts (id),
    -- Filled in when the first run fetches the video's details.
    title text,
    published_at timestamptz,
    duration_ms bigint CHECK (duration_ms > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, video_id)
);
CREATE INDEX episodes_published_idx ON episodes (published_at DESC NULLS LAST, id DESC);

-- One word-timed transcript of an episode. Every word is kept (transcript_words).
CREATE TABLE transcript_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id bigint NOT NULL REFERENCES episodes (id),
    source text NOT NULL CHECK (source IN ('parakeet', 'youtube', 'youtube_auto')),
    model text,
    language text,
    timing_digest text NOT NULL,
    word_count integer NOT NULL CHECK (word_count > 0),
    duration_ms bigint CHECK (duration_ms > 0),
    full_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (episode_id, timing_digest)
);
CREATE INDEX transcript_versions_episode_idx ON transcript_versions (episode_id, id DESC);
CREATE INDEX transcript_versions_fts_idx ON transcript_versions USING gin (to_tsvector('english', full_text));

CREATE TABLE transcript_words (
    transcript_version_id bigint NOT NULL REFERENCES transcript_versions (id),
    seq integer NOT NULL CHECK (seq > 0),
    word text NOT NULL,
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL,
    PRIMARY KEY (transcript_version_id, seq),
    CHECK (end_ms >= start_ms)
);

-- A clipping job, and the queue: the worker claims the oldest queued run.
CREATE TABLE runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id bigint NOT NULL REFERENCES episodes (id),
    instructions text NOT NULL DEFAULT '',  -- Highlightr "style"; '' is Highlightr's default
    transcript_version_id bigint REFERENCES transcript_versions (id),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'ready', 'failed')),
    stage text CHECK (stage IN ('fetching', 'captions', 'downloading', 'transcribing', 'identifying')),
    stage_started_at timestamptz,
    progress double precision,
    detail text,
    error text,
    attempts integer NOT NULL DEFAULT 0,
    params jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(params) = 'object'),  -- what Highlightr was sent, minus secrets
    created_at timestamptz NOT NULL DEFAULT now(),
    queued_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (episode_id, instructions)
);
CREATE INDEX runs_queue_idx ON runs (queued_at, id) WHERE status = 'queued';
CREATE INDEX runs_created_idx ON runs (created_at DESC, id DESC);

CREATE TABLE clips (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE,  -- unguessable share id: /c/<slug>
    run_id bigint NOT NULL REFERENCES runs (id),
    local_clip_id text,         -- Highlightr's clip_id
    rank integer NOT NULL,
    title text NOT NULL,
    summary text,
    start_ms bigint NOT NULL,
    end_ms bigint NOT NULL,
    first_word_seq integer NOT NULL,
    last_word_seq integer NOT NULL,
    overall_score double precision,
    details jsonb NOT NULL DEFAULT '{}',  -- the raw Highlightr clip
    has_thumb boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (start_ms >= 0 AND start_ms < end_ms AND first_word_seq <= last_word_seq)
);
CREATE INDEX clips_run_idx ON clips (run_id, rank);
CREATE INDEX clips_created_idx ON clips (created_at DESC, run_id, rank);

CREATE TABLE clip_quotes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id bigint NOT NULL REFERENCES clips (id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    quote_text text NOT NULL,
    first_word_seq integer,  -- null when the quote isn't found verbatim in the clip
    last_word_seq integer,
    UNIQUE (clip_id, ordinal)
);

-- ~20-second chunks of each clip, for full-text (and later semantic) search.
CREATE TABLE passages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id bigint NOT NULL REFERENCES clips (id),
    segmentation_version text NOT NULL,
    first_word_seq integer NOT NULL,
    last_word_seq integer NOT NULL,
    text text NOT NULL,
    text_digest text NOT NULL,
    UNIQUE (clip_id, segmentation_version, first_word_seq, last_word_seq)
);
CREATE INDEX passages_fts_idx ON passages USING gin (to_tsvector('english', text));
