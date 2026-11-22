"""The feed's picker (yolt/domain/feed.py): tiers, the play penalty, and the batch rules."""

import random
from collections import Counter

from yolt.domain.feed import DAY_SECONDS, DEFAULT_MIX, Candidate, mix, parse_mix, pick, select


def clip(clip_id, episode=1, days=0.5, plays=0):
    return Candidate(clip_id, episode, days * DAY_SECONDS, plays)


def test_mix_is_five_whole_numbers_up_to_100_not_all_zero():
    assert parse_mix("40,25,15,10,10") == (40, 25, 15, 10, 10)
    assert parse_mix(" 0, 0,0 ,0,100 ") == (0, 0, 0, 0, 100)
    for bad in ("", "40,25,15,10", "40,25,15,10,10,1", "0,0,0,0,0", "101,0,0,0,0", "-1,0,0,0,5", "1.5,1,1,1,1",
                "a,b,c,d,e", "²,1,1,1,1", "0100,1,1,1,1"):
        assert parse_mix(bad) is None, bad
    assert mix("nonsense") == DEFAULT_MIX


def test_tiers_follow_the_clip_age():
    assert [clip(1, days=d).tier for d in (0, 2.9, 3, 6.9, 7, 29, 30, 89, 90, 400)] == [0, 0, 1, 1, 2, 2, 3, 3, None, None]


def test_a_tier_is_chosen_by_weight_and_empty_tiers_drop_out():
    pool = [clip(1, days=1), clip(2, days=10), clip(3, days=200)]
    rng = random.Random(1)
    assert {pick(pool, (100, 0, 0, 0, 0), rng) for _ in range(50)} == {(0, "last_3_days")}
    assert {pick(pool, (0, 0, 100, 0, 0), rng) for _ in range(50)} == {(1, "last_30_days")}
    # Nothing in the last 7 days, so all its weight is gone, and "all time" draws from every clip.
    seen = Counter(pick(pool, (0, 100, 0, 0, 1), rng) for _ in range(300))
    assert set(seen) == {(0, "all_time"), (1, "all_time"), (2, "all_time")}
    # Every tier with clips weighted 0, and all time too: the whole pool rather than nothing.
    assert pick([clip(1, days=10)], (100, 0, 0, 0, 0), rng) == (0, "all_time")


def test_clips_played_less_come_up_more():
    pool = [clip(1, plays=0), clip(2, plays=1)]
    rng = random.Random(7)
    counts = Counter(pick(pool, (100, 0, 0, 0, 0), rng)[0] for _ in range(5000))
    assert 3.4 < counts[0] / counts[1] < 4.6  # 1 : 1/4


def test_a_batch_never_repeats_and_alternates_episodes():
    pool = [clip(i, episode=1 + i % 2) for i in range(1, 9)]
    picks = select(pool, recent=[], previous_episode=None, count=8, weights=DEFAULT_MIX, rng=random.Random(3))
    ids = [c.clip_id for c, _ in picks]
    assert sorted(ids) == list(range(1, 9))
    episodes = [c.episode_id for c, _ in picks]
    assert all(a != b for a, b in zip(episodes, episodes[1:]))


def test_recent_clips_and_the_last_episode_wait_their_turn():
    pool = [clip(i, episode=1 + i % 2) for i in range(1, 9)]
    recent = [2, 4, 6]  # episode 1
    for seed in range(20):
        picks = select(pool, recent=recent, previous_episode=1, count=5, weights=DEFAULT_MIX, rng=random.Random(seed))
        ids = [c.clip_id for c, _ in picks]
        assert picks[0][0].episode_id == 2
        assert not set(ids) & set(recent)


def test_a_small_catalogue_relaxes_the_rules_instead_of_stopping():
    one_episode = [clip(1), clip(2)]
    picks = select(one_episode, recent=[1, 2], previous_episode=1, count=5, weights=DEFAULT_MIX,
                   rng=random.Random(0))
    assert sorted(c.clip_id for c, _ in picks) == [1, 2]  # shorter than asked: only two clips
    assert select([], recent=[], previous_episode=None, count=3, weights=DEFAULT_MIX, rng=random.Random(0)) == []
