-- The step within a stage, for the stages made of several: while identifying, which of Highlightr's
-- steps the job is on (waiting, discovering, ranking, writing headlines), then Yolt saving the clips.

ALTER TABLE runs ADD COLUMN step text;
ALTER TABLE runs ADD COLUMN step_started_at timestamptz;
