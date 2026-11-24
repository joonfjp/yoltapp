-- A vector(1024) is about 4 KB, over the ~2 KB at which PostgreSQL moves a value out of the row into the
-- table's TOAST storage, so every semantic search (which compares the query with every stored vector) fetched
-- each one from there: 43k buffer reads for 7,200 vectors, and about twice the time. MAIN keeps a value in
-- the row unless the row would no longer fit in a page, which a 1024-dimension vector always does.
-- (Embeddings barely compress, so they're stored as they are.)
--
-- SET STORAGE applies to values written from now on; rewriting each one (through real[], so PostgreSQL
-- can't reuse the stored value as it is) moves the vectors already stored back into their rows. embedded_at
-- is left alone, so nothing counts as re-embedded and cached themes stay valid.

ALTER TABLE clip_vectors ALTER COLUMN embedding SET STORAGE MAIN;
ALTER TABLE passage_vectors ALTER COLUMN embedding SET STORAGE MAIN;
UPDATE clip_vectors SET embedding = embedding::real[]::vector;
UPDATE passage_vectors SET embedding = embedding::real[]::vector;

-- With the vectors in their rows, a row is a page, so anything that reads these tables for their other
-- columns (what's embedded with which model, and whether it's stale: GET /'s counts, the themes fingerprint,
-- the worker's pending check, each run's embedded clip count) would read every vector too: ~10x slower.
-- These indexes hold those columns, so such reads never touch the rows.
CREATE INDEX clip_vectors_lookup_idx ON clip_vectors (clip_id, model_name) INCLUDE (embedded_at);
CREATE INDEX passage_vectors_lookup_idx ON passage_vectors (passage_id, model_name) INCLUDE (text_digest, embedded_at);
