import pytest

from yolt.domain import cursor, ranges
from yolt.domain.diff import Item, diff


def items(*specs):
    return [Item(i, clip, first, last, (last - first + 1) * 100) for i, (clip, first, last) in enumerate(specs, 1)]


A, B, C, D = (1, 1, 10), (2, 1, 10), (3, 1, 10), (4, 1, 10)


def test_identical_revisions_have_no_changes():
    assert diff(items(A, B), items(A, B)) == {"added": [], "removed": [], "retrimmed": [], "moved": [],
                                               "duration_delta_ms": 0}


def test_insert_at_front_is_one_addition_not_a_cascade_of_moves():
    d = diff(items(D, A, B, C), items(A, B, C))
    assert [a["clip_id"] for a in d["added"]] == [4]
    assert d["moved"] == [] and d["removed"] == []


def test_swap_marks_one_move():
    d = diff(items(B, A, C), items(A, B, C))
    assert len(d["moved"]) == 1 and d["added"] == [] and d["removed"] == []


def test_retrim_and_removal_and_duration():
    d = diff(items((1, 2, 8), B), items(A, B, C))
    assert d["retrimmed"] == [{"clip_id": 1, "from": {"position": 1, "clip_id": 1, "words": {"first": 1, "last": 10}},
                               "to": {"position": 1, "clip_id": 1, "words": {"first": 2, "last": 8}}}]
    assert [r["clip_id"] for r in d["removed"]] == [3]
    assert d["duration_delta_ms"] == 700 + 1000 - 3000


def test_same_clip_twice_pairs_in_order():
    old = items((1, 1, 5), (1, 6, 9))
    new = items((1, 1, 5), (1, 6, 8))
    d = diff(new, old)
    assert d["retrimmed"][0]["from"]["words"] == {"first": 6, "last": 9}
    assert d["added"] == d["removed"] == []


def test_cursor_roundtrip_and_binding():
    filters = {"q": "mind", "sort": "-score"}
    token = cursor.encode([0.5, 42], filters)
    assert "=" not in token
    assert cursor.decode(token, filters) == [0.5, 42]
    with pytest.raises(cursor.CursorError, match="different filters"):
        cursor.decode(token, {**filters, "sort": "rank"})
    with pytest.raises(cursor.CursorError, match="malformed"):
        cursor.decode("not-a-cursor!!", filters)


TEXT = "Café — naïve".encode()  # é, —, ï are multi-byte


@pytest.mark.parametrize("header, expected", [
    ("bytes=0-3", (0, 3)),
    ("bytes=2-", (2, len(TEXT) - 1)),
    ("bytes=-4", (len(TEXT) - 4, len(TEXT) - 1)),
    ("bytes=0-999", (0, len(TEXT) - 1)),
    ("bytes=0-1,4-5", None),   # multi-range: ignored, whole body sent
    ("items=0-3", None),
    ("bytes=5-2", None),       # reversed: invalid, so ignored
])
def test_parse(header, expected):
    assert ranges.parse(header, len(TEXT)) == expected


@pytest.mark.parametrize("header", ["bytes=999-", "bytes=-0"])
def test_unsatisfiable(header):
    with pytest.raises(ranges.RangeError):
        ranges.parse(header, len(TEXT))


def test_widen_never_splits_a_character():
    # Bytes 3..4 are "é"; a range ending at byte 3 must extend to 4, one starting at 4 must back up to 3.
    assert TEXT[3:5].decode() == "é"
    assert ranges.widen(TEXT, 0, 3) == (0, 4)
    assert ranges.widen(TEXT, 4, 4) == (3, 4)
    for start in range(len(TEXT)):
        for end in range(start, len(TEXT)):
            a, b = ranges.widen(TEXT, start, end)
            TEXT[a:b + 1].decode("utf-8")  # never raises
