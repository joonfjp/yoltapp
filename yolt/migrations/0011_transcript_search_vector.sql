-- Keyword search over whole transcripts (GET /episodes?q=) ranks matches with ts_rank_cd, which needs
-- the tsvector itself. An expression index finds matches but can't hand the vector back, so every
-- search re-tokenized each transcript it looked at. The vector is now stored, computed once when a
-- transcript is written (and here, for the ones already stored), and the index is over it.

ALTER TABLE transcript_versions
    ADD COLUMN fts tsvector GENERATED ALWAYS AS (to_tsvector('english', full_text)) STORED;
DROP INDEX transcript_versions_fts_idx;
CREATE INDEX transcript_versions_fts_idx ON transcript_versions USING gin (fts);
