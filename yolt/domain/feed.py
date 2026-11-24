"""The feed's clip picker (the TV page), ported from RipeClips' feed.rs.

Each pick first chooses an age tier by its weight in the `feed_mix` setting (empty tiers drop out; "all time"
is the whole pool), then a clip in it, weighted towards clips the feed has played less: `1 / (1 + plays)²`, so a
clip played once is a quarter as likely as an unplayed one but never excluded. Age is the clip's, not the
episode's, so a new run shows up straight away.

A batch never repeats a clip, skips the clips shown most recently, and doesn't follow a clip with another from
the same episode. When a small or filtered catalogue runs out it relaxes those rules, the episode one first, so
a batch is only shorter than asked when there are fewer clips than that.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from functools import cached_property

DAY_SECONDS = 86_400
# (name, younger than this many days). Clips older than the last tier are only reachable through ALL_TIME.
TIERS: tuple[tuple[str, int], ...] = (("last_3_days", 3), ("last_7_days", 7), ("last_30_days", 30),
                                      ("last_90_days", 90))
ALL_TIME = "all_time"
TIER_NAMES: tuple[str, ...] = (*(name for name, _ in TIERS), ALL_TIME)
DEFAULT_MIX: tuple[int, ...] = (50, 30, 10, 5, 5)  # the feed_mix setting's default (yolt/db.py), in TIER_NAMES order
MAX_WEIGHT = 100
PLAY_PENALTY_EXPONENT = 2


@dataclass(frozen=True)
class Candidate:
    clip_id: int
    episode_id: int
    age_seconds: float
    plays: int

    # Computed once per candidate, not on every pick: a batch of 8 looks at each candidate up to 16 times, which
    # was most of the feed's time in a large catalogue. (cached_property stores past the frozen dataclass.)
    @cached_property
    def play_weight(self) -> float:
        return 1.0 / (1 + max(0, self.plays)) ** PLAY_PENALTY_EXPONENT

    @cached_property
    def tier(self) -> int | None:
        """Index into TIERS, or None when older than every tier."""
        return next((i for i, (_, days) in enumerate(TIERS) if self.age_seconds < days * DAY_SECONDS), None)


def parse_mix(value: str) -> tuple[int, ...] | None:
    """`feed_mix` ("50,30,10,5,5": one whole number 0–100 per tier, newest first, then all time) as weights,
    or None when it isn't that or every weight is 0."""
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != len(TIER_NAMES) or not all(p.isascii() and p.isdigit() and len(p) <= 3 for p in parts):
        return None
    weights = tuple(int(p) for p in parts)
    if any(w > MAX_WEIGHT for w in weights) or not any(weights):
        return None
    return weights


def mix(value: str) -> tuple[int, ...]:
    """The weights a setting stands for: an unusable value (only possible through the database) is the default."""
    return parse_mix(value) or DEFAULT_MIX


def _weighted_index(rng: random.Random, weights: list[float]) -> int | None:
    """An index chosen in proportion to `weights`, or None when every weight is 0."""
    total = sum(weights)
    if total <= 0:
        return None
    target = rng.random() * total
    last = None
    for index, weight in enumerate(weights):
        if weight <= 0:
            continue
        last = index
        if target < weight:
            return index
        target -= weight
    return last  # rounding left the target just past the end


def pick(pool: list[Candidate], weights: tuple[int, ...], rng: random.Random) -> tuple[int, str] | None:
    """One clip from `pool`: its index there, and the tier it was picked through."""
    if not pool:
        return None
    members: list[list[int]] = [[] for _ in TIERS]
    for index, candidate in enumerate(pool):
        if (tier := candidate.tier) is not None:
            members[tier].append(index)
    effective = [float(weights[t]) if members[t] else 0.0 for t in range(len(TIERS))] + [float(weights[-1])]
    tier = _weighted_index(rng, effective)
    if tier is None:  # every tier with clips in it is weighted 0: the whole pool
        tier = len(TIERS)
    chosen_from = members[tier] if tier < len(TIERS) else list(range(len(pool)))
    index = _weighted_index(rng, [pool[i].play_weight for i in chosen_from])
    return (chosen_from[index], TIER_NAMES[tier]) if index is not None else None


def select(candidates: list[Candidate], *, recent: list[int], previous_episode: int | None, count: int,
           weights: tuple[int, ...], rng: random.Random) -> list[tuple[Candidate, str]]:
    """Up to `count` picks, in play order. `recent` are the clips shown most recently (not picked again while
    others are left); `previous_episode` is the episode of the clip playing now, which the first pick avoids."""
    excluded = set(recent)
    picked: set[int] = set()
    previous = previous_episode
    out: list[tuple[Candidate, str]] = []
    while len(out) < count:
        unpicked: list[int] = []
        fresh: list[int] = []  # not shown recently
        separated: list[int] = []  # and not from the episode just picked
        for i, candidate in enumerate(candidates):  # one pass for all three: this runs once per pick
            if i in picked:
                continue
            unpicked.append(i)
            if candidate.clip_id not in excluded:
                fresh.append(i)
                if candidate.episode_id != previous:
                    separated.append(i)
        indexes = next((group for group in (separated, fresh, unpicked) if group), None)
        if indexes is None:
            break
        found = pick([candidates[i] for i in indexes], weights, rng)
        if found is None:
            break
        index = indexes[found[0]]
        candidate = candidates[index]
        picked.add(index)
        excluded.add(candidate.clip_id)
        previous = candidate.episode_id
        out.append((candidate, found[1]))
    return out
