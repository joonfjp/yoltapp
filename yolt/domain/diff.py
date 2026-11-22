"""Compare two revisions' item lists."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    position: int
    clip_id: int
    first: int
    last: int
    duration_ms: int

    def to_json(self) -> dict:
        return {"position": self.position, "clip_id": self.clip_id,
                "words": {"first": self.first, "last": self.last}}


def _lis(seq: list[int]) -> set[int]:
    """Indices of one longest strictly increasing subsequence of `seq`."""
    if not seq:
        return set()
    best_len, prev = [1] * len(seq), [-1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] < seq[i] and best_len[j] + 1 > best_len[i]:
                best_len[i], prev[i] = best_len[j] + 1, j
    i = max(range(len(seq)), key=best_len.__getitem__)
    out = set()
    while i != -1:
        out.add(i)
        i = prev[i]
    return out


def diff(new: list[Item], old: list[Item]) -> dict:
    """What changed going from `old` to `new`.

    Pass 1 pairs identical segments (same clip, same words), in order. Pass 2 pairs
    remaining items of the same clip whose words overlap (a re-trim). Unpaired items
    are added/removed. Identical pairs whose relative order changed are "moved" —
    computed as the pairs outside the longest order-preserving chain, so inserting
    one item at the front doesn't mark every later item as moved.
    """
    used_new: set[int] = set()
    exact: list[tuple[Item, Item]] = []
    for o in old:
        for i, n in enumerate(new):
            if i not in used_new and (n.clip_id, n.first, n.last) == (o.clip_id, o.first, o.last):
                used_new.add(i)
                exact.append((o, n))
                break
    paired_old = {id(o) for o, _ in exact}
    retrimmed: list[tuple[Item, Item]] = []
    for o in old:
        if id(o) in paired_old:
            continue
        for i, n in enumerate(new):
            if i not in used_new and n.clip_id == o.clip_id and n.first <= o.last and o.first <= n.last:
                used_new.add(i)
                paired_old.add(id(o))
                retrimmed.append((o, n))
                break
    exact.sort(key=lambda pair: pair[0].position)
    keep = _lis([n.position for _, n in exact])
    moved = [{"clip_id": n.clip_id, "from_position": o.position, "to_position": n.position}
             for k, (o, n) in enumerate(exact) if k not in keep]
    return {
        "added": [n.to_json() for i, n in enumerate(new) if i not in used_new],
        "removed": [o.to_json() for o in old if id(o) not in paired_old],
        "retrimmed": [{"clip_id": n.clip_id, "from": o.to_json(), "to": n.to_json()} for o, n in retrimmed],
        "moved": moved,
        "duration_delta_ms": sum(n.duration_ms for n in new) - sum(o.duration_ms for o in old),
    }
