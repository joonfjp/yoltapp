-- Whether a transcript has sentence punctuation. YouTube's auto-captions don't, and the checks that
-- read sentence ends (starts/ends mid-sentence, follows a question) would give wrong answers on them,
-- so they're skipped for unpunctuated transcripts. The rule: at least 1 sentence end per 100 words.

ALTER TABLE transcript_versions ADD COLUMN punctuated boolean;
UPDATE transcript_versions tv SET punctuated = (
    SELECT count(*) FILTER (WHERE w.word ~ '[.?!]["''”’)\]]*$') >= 0.01 * count(*)
    FROM transcript_words w WHERE w.transcript_version_id = tv.id);
ALTER TABLE transcript_versions ALTER COLUMN punctuated SET NOT NULL;
