from yolt.domain.segments import ClipWords, Word, check_overlaps, resolve
from yolt.problems import ErrorList


def clip(clip_id=1, transcript=10, first=5, last=9, gaps=(), zero=()):
    c = ClipWords(clip_id, transcript, first, last)
    for s in range(first, last + 1):
        if s not in gaps:
            start = s * 100
            c.words[s] = Word(s, f"w{s}", start, start if s in zero else start + 100)
    return c


def run(selection, c):
    errors = ErrorList()
    return resolve(selection, c, "/items/0", errors), [(e.code, e.pointer) for e in errors.items]


def test_whole_clip():
    seg, errs = run({"clip_id": 1}, clip())
    assert errs == []
    assert (seg.first, seg.last, seg.start_ms, seg.end_ms, seg.is_whole_clip) == (5, 9, 500, 1000, True)
    assert seg.text == "w5 w6 w7 w8 w9"
    assert seg.duration_ms == 500


def test_word_trim_derives_times_from_boundary_words():
    seg, errs = run({"clip_id": 1, "words": {"first": 6, "last": 7}}, clip())
    assert errs == [] and (seg.start_ms, seg.end_ms, seg.is_whole_clip) == (600, 800, False)


def test_reversed_words():
    _, errs = run({"clip_id": 1, "words": {"first": 8, "last": 6}}, clip())
    assert errs == [("words_reversed", "/items/0/words/last")]


def test_outside_both_ends_reports_both():
    _, errs = run({"clip_id": 1, "words": {"first": 4, "last": 10}}, clip())
    assert errs == [("words_outside_clip", "/items/0/words/first"), ("words_outside_clip", "/items/0/words/last")]


def test_unknown_clip():
    seg, errs = run({"clip_id": 99}, None)
    assert seg is None and errs == [("clip_not_found", "/items/0/clip_id")]


def test_missing_timed_word_in_range():
    _, errs = run({"clip_id": 1, "words": {"first": 5, "last": 8}}, clip(gaps={7}))
    assert errs == [("words_missing", "/items/0/words")]
    _, errs = run({"clip_id": 1}, clip(gaps={7}))
    assert errs == [("words_missing", "/items/0/clip_id")]


def test_zero_duration_is_rejected():
    _, errs = run({"clip_id": 1, "words": {"first": 6, "last": 6}}, clip(zero={6}))
    assert errs == [("invalid_value", "/items/0")]


def test_text_anchor_errors_point_at_the_field():
    _, errs = run({"clip_id": 1, "text": {"from": "w7", "to": "nope"}}, clip())
    assert errs == [("text_not_found", "/items/0/text/to")]


def resolved(*specs):
    out = []
    for transcript, first, last, clip_id in specs:
        errors = ErrorList()
        out.append(resolve({"clip_id": clip_id, "words": {"first": first, "last": last}},
                           clip(clip_id, transcript, 1, 100), "/items/0", errors))
    return out


def overlaps(*specs):
    errors = ErrorList()
    check_overlaps(resolved(*specs), "items", errors)
    return [(e.code, e.pointer) for e in errors.items]


def test_same_clip_twice_without_overlap_is_allowed():
    # Real data does this (compilation 16 uses two parts of clip 762).
    assert overlaps((10, 1, 5, 1), (10, 6, 9, 1)) == []


def test_overlap_within_a_transcript_is_rejected_even_across_clips():
    assert overlaps((10, 1, 5, 1), (10, 5, 9, 2)) == [("overlapping_segments", "/items/1")]


def test_same_words_in_different_transcripts_are_fine():
    assert overlaps((10, 1, 5, 1), (11, 1, 5, 2)) == []


def test_unresolved_items_are_skipped():
    errors = ErrorList()
    check_overlaps([None, *resolved((10, 1, 5, 1))], "items", errors)
    assert errors.items == []
