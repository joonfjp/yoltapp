from yolt import transcript


def verbose(text, fragments):
    t = 0.0
    words = []
    for fragment in fragments:
        words.append({"word": fragment, "start": t, "end": t + 0.4})
        t += 0.5
    return {"text": text, "words": words, "duration": t}


def test_timed_words_aligns_subword_fragments_to_punctuated_words():
    data = verbose("Hello, world. It's fine!", ["Hel", "lo", "world", "it", "'s", "fine"])
    words = transcript.timed_words(data)
    assert [w["text"] for w in words] == ["Hello,", "world.", "It's", "fine!"]
    assert words[0]["start_seconds"] == 0.0 and words[0]["end_seconds"] == 0.9
    assert words[2]["start_seconds"] == 1.5


def test_timed_words_falls_back_to_fragments_when_alignment_fails():
    data = verbose("Totally different text", ["alpha", "beta"])
    assert [w["text"] for w in transcript.timed_words(data)] == ["alpha", "beta"]


def test_rows_break_on_sentences_and_render_srt():
    words = transcript.timed_words(verbose("One two. Three four.", ["one", "two", "three", "four"]))
    rows = transcript.make_rows(words)
    assert [r["text"] for r in rows] == ["One two.", "Three four."]
    srt = transcript.rows_to_srt(rows)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:00,900\nOne two.\n\n2\n00:00:01,000")


def test_srt_timestamp_hours():
    assert transcript.srt_timestamp(3725.5) == "01:02:05,500"


def test_caption_words_auto_track_has_word_timing():
    data = {"events": [
        {"tStartMs": 320, "dDurationMs": 14000, "segs": [{"utf8": "[Music]"}]},
        {"tStartMs": 18800, "dDurationMs": 7160, "segs": [
            {"utf8": "We're"}, {"utf8": " no", "tOffsetMs": 239}, {"utf8": " strangers.", "tOffsetMs": 559}]},
        {"tStartMs": 25000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 30000, "dDurationMs": 1000, "segs": [{"utf8": "Love"}]},
    ]}
    words = transcript.caption_words(data)
    assert [w["text"] for w in words] == ["We're", "no", "strangers.", "Love"]
    assert words[0] == {"text": "We're", "start_seconds": 18.8, "end_seconds": 19.039}
    assert words[2]["end_seconds"] == 20.359  # capped at 1s, not stretched to the next line
    assert words[3]["end_seconds"] == 31.0
    rows = transcript.make_rows(words)
    assert rows[0]["text"] == "We're no strangers."


def test_caption_words_manual_track_spreads_cue_timing():
    data = {"events": [
        {"tStartMs": 1360, "dDurationMs": 1680, "segs": [{"utf8": "[♪♪♪]"}]},
        {"tStartMs": 10000, "dDurationMs": 2000, "segs": [{"utf8": "♪ You know\nthe rules ♪"}]},
        {"tStartMs": 12000, "dDurationMs": 1000, "segs": [{"utf8": ">> And so do I"}]},
    ]}
    words = transcript.caption_words(data)
    assert [w["text"] for w in words] == ["You", "know", "the", "rules", "And", "so", "do", "I"]
    assert words[0]["start_seconds"] == 10.0
    assert words[3]["end_seconds"] == 12.0
    assert words[4]["start_seconds"] == 12.0


def test_caption_words_empty_track():
    import pytest
    with pytest.raises(transcript.TranscriptError):
        transcript.caption_words({"events": [{"tStartMs": 0, "dDurationMs": 10, "segs": [{"utf8": "[Music]"}]}]})
