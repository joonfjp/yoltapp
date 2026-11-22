-- Compilations: ordered selections of clip word ranges, kept as immutable revisions.
--
-- A row in `compilations` is one revision. A compilation is every revision sharing a title,
-- numbered 1, 2, …; its ID is its first revision's ID (the clipit convention, so the API is
-- the same). Items address words; their times and transcript are derived and stored with them.
-- Nothing in the API updates or deletes a revision, its items, or a render record.

CREATE TABLE compilations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    revision integer NOT NULL CHECK (revision >= 1),
    metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (title, revision)
);

CREATE TABLE compilation_items (
    compilation_id bigint NOT NULL REFERENCES compilations (id),
    "position" integer NOT NULL CHECK ("position" >= 1),
    clip_id bigint NOT NULL REFERENCES clips (id),
    transcript_version_id bigint NOT NULL REFERENCES transcript_versions (id),
    first_word_seq integer NOT NULL,
    last_word_seq integer NOT NULL,
    start_ms bigint NOT NULL,
    end_ms bigint NOT NULL,
    PRIMARY KEY (compilation_id, "position"),
    -- The boundary words must exist in the item's transcript.
    FOREIGN KEY (transcript_version_id, first_word_seq) REFERENCES transcript_words (transcript_version_id, seq),
    FOREIGN KEY (transcript_version_id, last_word_seq) REFERENCES transcript_words (transcript_version_id, seq),
    CHECK (first_word_seq <= last_word_seq AND start_ms < end_ms)
);
CREATE INDEX compilation_items_clip_idx ON compilation_items (clip_id);

-- A render of a revision made outside Yolt, recorded so clip usage counts what shipped.
CREATE TABLE compilation_renders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id bigint NOT NULL REFERENCES compilations (id),
    format text NOT NULL CHECK (format IN ('long', 'short')),
    output_path text NOT NULL,
    duration_ms bigint NOT NULL CHECK (duration_ms > 0),
    spec_path text,
    rendered_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX compilation_renders_revision_idx ON compilation_renders (revision_id);

CREATE TABLE compilation_render_positions (
    render_id bigint NOT NULL REFERENCES compilation_renders (id),
    "position" integer NOT NULL CHECK ("position" >= 1),
    PRIMARY KEY (render_id, "position")
);
