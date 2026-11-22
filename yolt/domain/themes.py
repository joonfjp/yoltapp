"""Themes: deterministic clusters of clip vectors, described by their clips rather than named.

Spherical k-means (cosine) with k-means++ seeding from a fixed seed, so the same vectors and k
always give the same themes. A theme is labelled by its most central clips and by keywords that
are frequent in it and rare in the other themes (class-based TF-IDF over titles and summaries).
Naming is left to the caller: the API never calls an LLM.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

SEED = 0
MAX_ITERATIONS = 100
KEYWORDS = 6
REPRESENTATIVES = 3

STOPWORDS = frozenset("""
a about above after again against all almost also am an and any are aren't around as at back be because been
before being below between both but by can can't could did didn't do does doesn't doing don't down during each
even ever every few for from further get gets getting got had has hasn't have haven't having he he's her here
hers herself him himself his how i i'm if in into is isn't it it's its itself just keep know let like make makes
making many may me might more most much must my myself new no nor not now of off often on once one only or other
our ours ourselves out over own really same say says she should shouldn't so some still such than that that's the
their theirs them themselves then there there's these they they're thing things this those though through to too
two under until up upon us very was wasn't way we we're were weren't what what's when where which while who whom
whose why will with without won't would wouldn't yes yet you you're your yours yourself yourselves
apparently actually basically quite simply suddenly turns
""".split())


@dataclass(frozen=True)
class Clustering:
    labels: np.ndarray  # theme index per input row
    centroids: np.ndarray  # k x d, unit length
    similarity: np.ndarray  # each row's cosine similarity to its own centroid


def normalize(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.where(norms == 0, 1, norms)


def cluster(vectors: np.ndarray, k: int, seed: int = SEED) -> Clustering:
    """Spherical k-means. `vectors` must be in a stable row order for the result to be stable."""
    x = normalize(vectors.astype(np.float64))
    n = len(x)
    if not 1 <= k <= n:
        raise ValueError(f"k must be between 1 and {n}")
    rng = np.random.default_rng(seed)
    centers = [int(rng.integers(n))]
    closest = x @ x[centers[0]]
    for _ in range(1, k):  # k-means++: pick far points, weighted by squared cosine distance
        weight = np.clip(1 - closest, 0, None) ** 2
        nxt = int(rng.choice(n, p=weight / weight.sum())) if weight.sum() > 0 else int(np.argmin(closest))
        centers.append(nxt)
        closest = np.maximum(closest, x @ x[nxt])
    centroids = x[centers].copy()
    labels = np.full(n, -1)
    for _ in range(MAX_ITERATIONS):
        sims = x @ centroids.T
        new = sims.argmax(axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        sizes = np.bincount(labels, minlength=k)
        own = sims[np.arange(n), labels]
        for j in np.flatnonzero(sizes == 0):
            # An emptied theme restarts at the point its centroid explains worst, taken from a theme that
            # can spare it, so restarting never empties another one.
            spare = np.flatnonzero(sizes[labels] > 1)
            if not len(spare):
                break
            worst = int(spare[np.argmin(own[spare])])
            sizes[labels[worst]] -= 1
            labels[worst], sizes[j], own[worst] = j, 1, 1.0
        for j in range(k):
            members = x[labels == j]
            if len(members):
                centroids[j] = members.sum(axis=0)
        centroids = normalize(centroids)
    similarity = (x * centroids[labels]).sum(axis=1)
    return Clustering(labels, centroids, similarity)


def tokens(text: str) -> set[str]:
    words = (w.strip("'").removesuffix("'s") for w in re.findall(r"[a-z][a-z']+", text.lower()))
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def keywords(texts_by_theme: list[list[str]], top: int = KEYWORDS) -> list[list[str]]:
    """Per theme, the words most frequent in its clips and rarest across the other themes."""
    counts = [Counter(t for text in texts for t in tokens(text)) for texts in texts_by_theme]
    spread = Counter(t for c in counts for t in c)  # how many themes use each word
    k = len(texts_by_theme)
    out = []
    for texts, c in zip(texts_by_theme, counts, strict=True):
        size = max(len(texts), 1)
        minimum = 2 if size >= 4 else 1
        scored = sorted(((n / size) * math.log(1 + k / spread[t]), t) for t, n in c.items() if n >= minimum)
        out.append([t for _, t in sorted(scored, key=lambda s: (-s[0], s[1]))[:top]])
    return out
