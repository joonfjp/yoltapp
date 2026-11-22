-- Semantic search: one embedding per clip (its title, summary and quotes) and per passage.
-- A row carries the model that made it (`<model>@1024`) and a digest of the text it embedded, so
-- a new model or changed text makes it pending again; the worker re-embeds it in place.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE clip_vectors (
    clip_id bigint PRIMARY KEY REFERENCES clips (id),
    model_name text NOT NULL,
    text_digest text NOT NULL,
    embedding vector(1024) NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE passage_vectors (
    passage_id bigint PRIMARY KEY REFERENCES passages (id),
    model_name text NOT NULL,
    text_digest text NOT NULL,
    embedding vector(1024) NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now()
);
