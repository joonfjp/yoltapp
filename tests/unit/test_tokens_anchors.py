import pytest

from yolt.domain.anchors import AnchorErrors, resolve_anchors
from yolt.domain.tokens import normalize, tokenize_words


@pytest.mark.parametrize("raw, expected", [
    ("Like,", ["like"]),
    ("that's", ["thats"]),
    ("That’s", ["thats"]),          # curly apostrophe
    ("winner-", ["winner"]),        # ASR hyphen fragment
    ("well-known", ["well", "known"]),
    ("our$", ["our"]),              # ASR artifact
    ("—", []),
    ("Café", ["café"]),
    ("ＡＢＣ", ["abc"]),            # NFKC folds full-width letters
    ("  You know, you know  ", ["you", "know", "you", "know"]),
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_tokenize_words_maps_subtokens_to_their_word():
    tokens = tokenize_words([(10, "well-known"), (11, "story.")])
    assert [(t.norm, t.seq) for t in tokens] == [("well", 10), ("known", 10), ("story", 11)]


def words(text: str, start: int = 1):
    return tokenize_words(list(enumerate(text.split(), start)))


CLIP = words("You know, you know, you know what I mean? That is the thing, you know.", start=100)


def test_from_and_to():
    assert resolve_anchors(CLIP, 1, "what I mean", "the thing") == (106, 112)


def test_only_from_keeps_clip_end():
    assert resolve_anchors(CLIP, 1, "What I MEAN?", None) == (106, CLIP[-1].seq)


def test_only_to_keeps_clip_start():
    assert resolve_anchors(CLIP, 1, None, "the thing") == (100, 112)


def test_to_is_searched_only_after_from():
    # "you know" occurs 4 times in the clip but only once after "the thing".
    assert resolve_anchors(CLIP, 1, "the thing", "you know") == (111, 114)


def test_ambiguous_from_reports_count():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors(CLIP, 7, "you know", None)
    (err,) = exc.value.errors
    assert (err.field, err.code) == ("from", "text_ambiguous")
    assert "4 times in clip 7" in err.message


def test_ambiguous_to_without_from():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors(CLIP, 1, None, "you know")
    assert exc.value.errors[0].code == "text_ambiguous"


def test_to_only_before_from_is_out_of_order():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors(CLIP, 1, "the thing", "what I mean")
    assert exc.value.errors[0].code == "text_out_of_order"


def test_to_may_not_overlap_from():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors(CLIP, 1, "that is the thing", "the thing")
    assert exc.value.errors[0].code == "text_out_of_order"


def test_every_problem_is_reported():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors(CLIP, 1, "nowhere to be found", "also missing")
    assert [(e.field, e.code) for e in exc.value.errors] == [("from", "text_not_found"), ("to", "text_not_found")]


def test_punctuation_only_phrase_is_invalid():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors(CLIP, 1, "...", None)
    assert exc.value.errors[0].code == "invalid_value"


def test_phrases_match_across_hyphen_fragments():
    tokens = words("back to the well-known story of the winner- take-all brain.", start=1)
    assert resolve_anchors(tokens, 1, "well known story", "winner take all") == (4, 9)


def test_clip_without_timed_words():
    with pytest.raises(AnchorErrors) as exc:
        resolve_anchors([], 3, "x", None)
    assert exc.value.errors[0].code == "words_missing"
