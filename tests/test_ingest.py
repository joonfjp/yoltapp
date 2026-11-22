"""Pure catalogue logic: word numbering, clip word ranges, quotes, passages."""

from yolt import ingest, transcript
from yolt.ingest import Word


def words(*spans: tuple[str, int, int]) -> list[Word]:
    return [Word(seq, text, start, end) for seq, (text, start, end) in enumerate(spans, 1)]


WORDS = words(("So", 0, 300), ("here's", 400, 800), ("the", 900, 1000), ("thing.", 1100, 1500),
              ("—", 1500, 1500), ("Nobody", 3000, 3400), ("saw", 3500, 3800), ("it.", 3900, 4200))


def test_word_rows_number_from_one_in_ms():
    rows = ingest.word_rows([
        {"text": "Hi", "start_seconds": 0.0004, "end_seconds": 0.25},
        {"text": "there.", "start_seconds": 0.3, "end_seconds": 0.2999},  # ends before it starts
    ])
    assert rows == [Word(1, "Hi", 0, 250), Word(2, "there.", 300, 300)]
    assert ingest.as_seconds(rows)[0] == {"text": "Hi", "start_seconds": 0.0, "end_seconds": 0.25}


def test_timing_digest_tracks_words_and_times():
    same = ingest.timing_digest(WORDS)
    assert same == ingest.timing_digest(list(WORDS))
    moved = [*WORDS[:-1], Word(8, "it.", 3900, 4300)]
    assert ingest.timing_digest(moved) != same


def test_word_bounds():
    assert ingest.word_bounds(WORDS, 0, 1500) == (1, 4)  # the dash at 1500 only touches the end
    assert ingest.word_bounds(WORDS, 0, 1600) == (1, 5)
    assert ingest.word_bounds(WORDS, 750, 3450) == (2, 6)  # partly covered words count
    assert ingest.word_bounds(WORDS, 1600, 2900) is None  # a silence
    assert ingest.word_bounds(WORDS, 5000, 6000) is None


def test_word_bounds_leave_out_words_that_only_touch_the_span():
    contiguous = words(("one", 0, 500), ("two", 500, 1000), ("three", 1000, 1500), ("four", 1500, 2000))
    assert ingest.word_bounds(contiguous, 500, 1500) == (2, 3)
    assert ingest.word_bounds(contiguous, 1000, 1500) == (3, 3)  # exactly one word
    assert ingest.word_bounds(contiguous, 1200, 1300) == (3, 3)  # inside one word
    # words that take no time (rounded to one millisecond) count when they fall inside, at the start too
    instant = words(("a", 0, 500), ("b", 500, 500), ("c", 500, 500), ("d", 900, 1200))
    assert ingest.word_bounds(instant, 500, 700) == (2, 3)
    assert ingest.word_bounds(instant, 0, 500) == (1, 1)


def test_a_clip_on_caption_row_edges_gets_exactly_those_rows_words():
    # Caption words are contiguous (each ends where the next starts); Highlightr's clips are row edges.
    text = "one two three four. five six seven eight. nine ten eleven twelve."
    contiguous = words(*[(t, i * 400, (i + 1) * 400) for i, t in enumerate(text.split())])
    rows = transcript.make_rows(ingest.as_seconds(contiguous))
    assert rows[1]["text"] == "five six seven eight."
    clip, = ingest.build_clips({"clips": [{"rank": 1, "start_seconds": rows[1]["start_seconds"],
                                          "end_seconds": rows[1]["end_seconds"]}]}, contiguous)
    assert (clip.first_word_seq, clip.last_word_seq) == (5, 8)
    assert clip.passages[0].text == "five six seven eight."


def test_quote_range_ignores_case_and_punctuation():
    assert ingest.quote_range("Here's the THING", WORDS, 1, 8) == (2, 4)
    assert ingest.quote_range("thing nobody", WORDS, 1, 8) == (4, 6)  # skips the dash
    assert ingest.quote_range("nobody saw", WORDS, 1, 4) is None  # outside the range
    assert ingest.quote_range("...", WORDS, 1, 8) is None


def test_passages_cover_the_range_in_20s_chunks():
    long = words(*[(f"w{i}", i * 1000, i * 1000 + 900) for i in range(50)])
    chunks = ingest.passages(long, 3, 47)
    assert [(p.first_word_seq, p.last_word_seq) for p in chunks] == [(3, 22), (23, 42), (43, 47)]
    assert chunks[0].text.startswith("w2 w3") and chunks[0].text_digest == ingest.sha256(chunks[0].text)


def test_build_clips():
    result = {"clips": [
        {"rank": 2, "start_seconds": 3.0, "end_seconds": 4.2, "summary": "s", "title": "Second",
         "best_quotes": ["saw it", "never said"], "scores": {"overall": 0.5}},
        {"rank": 1, "clip_id": "clip_001", "start_seconds": 0, "end_seconds": 1.5, "title": "Plain title",
         "best_headline": "Headline", "summary": "plain", "best_description": "d"},
        {"rank": 3, "start_seconds": 1.6, "end_seconds": 2.9, "title": "Silence"},
    ]}
    first, second = ingest.build_clips(result, WORDS)
    assert (first.rank, first.title, first.summary, first.local_clip_id) == (1, "Headline", "d", "clip_001")
    assert (first.start_ms, first.end_ms, first.first_word_seq, first.last_word_seq) == (0, 1500, 1, 4)
    assert first.passages[0].text == "So here's the thing."  # not the dash just after it
    assert (second.title, second.summary, second.overall_score) == ("Second", "s", 0.5)
    assert (second.first_word_seq, second.last_word_seq) == (6, 8)
    assert [(q.ordinal, q.text, q.first_word_seq, q.last_word_seq) for q in second.quotes] == [
        (1, "saw it", 7, 8), (2, "never said", None, None)]
    assert first.details["title"] == "Plain title"
    assert first.slug != second.slug


def test_punctuated_needs_one_sentence_end_per_hundred_words():
    bare = [Word(i, "word", i, i) for i in range(1, 201)]
    assert ingest.punctuated(bare) is False
    one_end = [*bare[:-1], Word(200, "end.", 200, 200)]
    assert ingest.punctuated(one_end) is False  # 1 in 200
    two_ends = [Word(1, 'said."', 0, 0), *one_end[1:]]
    assert ingest.punctuated(two_ends) is True  # 2 in 200, closing quote allowed
    assert ingest.punctuated([]) is False
