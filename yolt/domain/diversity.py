"""Maximal marginal relevance: re-rank so each pick is relevant and unlike the picks before it."""
from __future__ import annotations

# Weight on relevance; the rest penalizes similarity to what's already picked. Tuned on the dev
# catalogue (2026-10-24): at 0.7 the closest pair in a top 10 drops from about 0.75-0.8 to about
# 0.6, for 0.01-0.05 less mean similarity to the query.
LAMBDA = 0.7


def mmr(candidates: list[tuple[int, float]], similarity: dict[tuple[int, int], float], k: int,
        lam: float = LAMBDA) -> list[int]:
    """Pick up to k ids from (id, relevance) candidates, best first.

    Relevance must be on the same scale as `similarity` (cosine), which holds both orderings of
    each pair that has vectors. A pair with no similarity counts as unrelated."""
    relevance = dict(candidates)
    redundancy = {i: 0.0 for i, _ in candidates}
    remaining = [i for i, _ in candidates]
    picked: list[int] = []
    while remaining and len(picked) < k:
        best = max(remaining, key=lambda i: (lam * relevance[i] - (1 - lam) * redundancy[i], relevance[i], -i))
        picked.append(best)
        remaining.remove(best)
        for i in remaining:
            redundancy[i] = max(redundancy[i], similarity.get((i, best), 0.0))
    return picked


def cosine_scale(ranked_similarities: list[float | None]) -> list[float]:
    """Relevance on the cosine scale for candidates in ranked order: the n-th best similarity for the
    n-th candidate. For semantic ranking that is each candidate's own similarity; for hybrid it keeps
    the fused order while making relevance comparable with redundancy."""
    scale = sorted((s for s in ranked_similarities if s is not None), reverse=True) or [0.0]
    return [scale[min(n, len(scale) - 1)] for n in range(len(ranked_similarities))]
