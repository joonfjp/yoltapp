-- Keyword search over clips (GET /clips?q=, hybrid search, the TV's topic, facets) ranks the matching
-- passages with ts_rank_cd, which needs the tsvector itself. As with transcripts in 0011, the expression
-- index found the matches but every search re-tokenized each matched passage to rank it (a common word
-- took ~50 ms on 2,400 clips instead of ~3). The vector is now stored and the index is over it.

ALTER TABLE passages
    ADD COLUMN fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;
DROP INDEX passages_fts_idx;
CREATE INDEX passages_fts_idx ON passages USING gin (fts);
