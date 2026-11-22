"""The editorial checks and the MMR re-ranking, on hand-built inputs."""
import math

import pytest

from yolt.domain import review as checks
from yolt.domain.context import ends_mid_sentence
from yolt.domain.diversity import cosine_scale, mmr


def vec(*xs):
    return list(xs) + [0.0] * (4 - len(xs))


def at(angle_deg):
    """A unit vector whose cosine with vec(1) is cos(angle)."""
    a = math.radians(angle_deg)
    return vec(math.cos(a), math.sin(a))


def item(position, *, clip=None, episode=1, ms=20_000, text="A whole sentence.", before="Earlier.", speakers=(),
         vector=None, alignment="exact"):
    return checks.Item(position=position, clip_id=clip or position, episode_id=episode, duration_ms=ms, text=text,
                       before=before, alignment=alignment, speakers=list(speakers), vector=vector)


def codes(body):
    return [(f["code"], f["positions"]) for f in body["findings"]]


def run(items, target=None, vectors=True):
    return checks.review(items, target_duration_ms=target, vectors=vectors)


def test_near_duplicate_threshold_and_same_clip_exemption():
    close, apart = at(math.degrees(math.acos(0.76))), at(math.degrees(math.acos(0.74)))
    assert codes(run([item(1, vector=vec(1)), item(2, vector=close, episode=2)])) == [("near_duplicate", [1, 2])]
    assert codes(run([item(1, vector=vec(1)), item(2, vector=apart, episode=2)])) == []
    assert codes(run([item(1, clip=7, vector=vec(1)), item(2, clip=7, vector=vec(1))])) == []


def test_off_theme_needs_a_low_score_well_below_the_typical_item():
    tight = [item(n, vector=at(d), episode=n) for n, d in ((1, 0), (2, 10), (3, 20), (4, 15))]
    stray = item(5, vector=at(80), episode=5)
    body = run(tight + [stray])
    assert ("off_theme", [5]) in codes(body)
    assert body["items"][4]["theme_similarity"] < checks.OFF_THEME_BELOW
    assert "off_theme" not in {c for c, _ in codes(run(tight))}
    two = run([item(1, vector=vec(1)), item(2, vector=vec(0, 1), episode=2)])
    assert all(i["theme_similarity"] is None for i in two["items"])  # needs 3 embedded items


def test_cut_points():
    body = run([item(1, before="and then", text="it fell apart"), item(2, before="Why did it fail?", episode=2),
                item(3, before="", text="Opening line.", episode=3)])
    assert codes(body) == [("ends_mid_sentence", [1]), ("starts_mid_sentence", [1]), ("follows_question", [2])]
    assert body["items"][2]["starts_mid_sentence"] is False  # nothing before the start of a transcript
    assert "approximate" in run([item(1, before="and", alignment="approximate")])["findings"][0]["message"]
    assert ends_mid_sentence('He said "stop."') is False and ends_mid_sentence("and so") is True


def test_balance_by_episode_and_speaker():
    same_episode = [item(n, episode=1, speakers=["Ada"]) for n in (1, 2, 3)]
    assert codes(run(same_episode)) == [("episode_heavy", [1, 2, 3])]  # not also speaker_heavy
    spread = [item(n, episode=n, speakers=["Ada"] if n <= 3 else ["Bo"]) for n in range(1, 6)]
    body = run(spread)
    assert codes(body) == [("speaker_heavy", [1, 2, 3])] and "(Ada)" in body["findings"][0]["message"]
    assert codes(run([item(n, episode=n % 2) for n in range(1, 6)])) == [("episode_heavy", [1, 3, 5])]
    assert codes(run([item(1), item(2)])) == []  # fewer than 3 never counts


def test_length_checks_and_ordering():
    body = run([item(1, ms=2_000, episode=1), item(2, ms=100_000, episode=2, before="and")], target=60_000)
    assert codes(body) == [("duration_off_target", []), ("starts_mid_sentence", [2]), ("item_too_short", [1]),
                           ("item_too_long", [2])]  # warnings first; whole-list findings before item findings
    assert codes(run([item(1, ms=27_000), item(2, ms=28_000, episode=2)], target=60_000)) == []  # within 10%
    # With a target, "too long" is more than half of it, so a 30-60 s supercut flags a 35 s item.
    assert codes(run([item(1, ms=35_000), item(2, ms=20_000, episode=2)], target=60_000)) == [("item_too_long", [1])]
    assert codes(run([item(1, ms=80_000)])) == []  # 90 s without a target
    # A long target allows long items: half of a 10-minute one is 5 minutes, not 90 s.
    assert codes(run([item(1, ms=120_000), item(2, ms=301_000, episode=2), item(3, ms=179_000, episode=3)],
                     target=600_000)) == [("item_too_long", [2])]
    assert body["duration_ms"] == 102_000 and body["target_duration_ms"] == 60_000


def test_without_vectors_the_semantic_checks_are_skipped():
    body = run([item(1), item(2, episode=2)], vectors=False)
    assert body["skipped"] == list(checks.SEMANTIC_CHECKS) and body["findings"] == []


def test_mmr_trades_a_little_relevance_for_less_repetition():
    sim = {}
    for a, b, s in ((1, 2, 0.95), (1, 3, 0.4), (2, 3, 0.4)):
        sim[(a, b)] = sim[(b, a)] = s
    assert mmr([(1, 0.62), (2, 0.61), (3, 0.58)], sim, 2) == [1, 3]
    assert mmr([(1, 0.62), (2, 0.61), (3, 0.20)], sim, 2) == [1, 2]  # nothing else is close enough
    assert mmr([(1, 0.6), (2, 0.5)], {}, 5) == [1, 2] and mmr([], {}, 3) == []


def test_cosine_scale_keeps_order_and_borrows_similarities():
    assert cosine_scale([0.6, 0.5, 0.4]) == [0.6, 0.5, 0.4]
    assert cosine_scale([None, 0.3, 0.5]) == [0.5, 0.3, 0.3]  # hybrid: a keyword-only hit ranked first
    assert cosine_scale([None]) == [0.0]


@pytest.mark.parametrize("sim, flagged", [(0.75, True), (0.7499, False)])
def test_threshold_is_inclusive(sim, flagged):
    other = at(math.degrees(math.acos(sim)))
    assert (("near_duplicate", [1, 2]) in codes(run([item(1, vector=vec(1)), item(2, vector=other, episode=2)]))) is flagged
