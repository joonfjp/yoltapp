-- Speakers: the people featured in an episode, inferred once per episode and correctable.
-- A speaker is a person by name, shared across episodes, so `speaker_id` filters work
-- across the whole catalogue. Attribution is per episode, never per clip.

CREATE TABLE speakers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200)
);
CREATE UNIQUE INDEX speakers_name_idx ON speakers (lower(name));

CREATE TABLE episode_speakers (
    episode_id bigint NOT NULL REFERENCES episodes (id),
    speaker_id bigint NOT NULL REFERENCES speakers (id),
    role text NOT NULL CHECK (role IN ('host', 'guest')),
    attribution_method text NOT NULL,  -- llm_metadata_inference | manual
    verified boolean NOT NULL DEFAULT false,
    PRIMARY KEY (episode_id, speaker_id)
);
CREATE INDEX episode_speakers_speaker_idx ON episode_speakers (speaker_id);

-- The inference input that isn't stored elsewhere, and whether inference has run (finding nobody
-- is a result too). Null means pending: the worker tries again when it's idle.
ALTER TABLE episodes ADD COLUMN description text;
ALTER TABLE episodes ADD COLUMN speakers_inferred_at timestamptz;
