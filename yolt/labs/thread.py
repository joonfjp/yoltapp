"""Thread: an idea in, an ordered reel of clips out.

The clips that best match the idea by words and meaning (what GET /clips?search=hybrid&diversify=true
returns, a few per episode) are put in an order where each leads into the next: the best match opens,
then each next clip is the remaining one most like the clip before it. A clip that overlaps one already
picked is left out, so the draft is always a valid compilation. It's reviewed like one (POST
/segments/review); POST /compilations saves it.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, Query

from .. import api
from ..embeddings import MAX_QUERY_CHARS
from ..repo import clips as clips_repo
from ..repo import compilations as comp_repo
from ..repo import review as review_repo
from ..repo import vectors as vector_repo
from .space import load

LAB = "thread"
MAX_CLIPS = 12
MAX_TARGET_MS = 86_400_000
router = APIRouter()


def idea(db: api.Db, embed: api.Embed, q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_CHARS)]) -> str:
    """The idea, embedded before the request takes its connection (see api._embed_early)."""
    api._embed_early(db, embed, q)
    return q


@router.get("/labs/thread")
def get_thread(q: Annotated[str, Depends(idea)], conn: api.Conn, embed: api.Embed, base: api.Base,
               limit: Annotated[int, Query(ge=2, le=MAX_CLIPS)] = 6,
               max_per_episode: Annotated[int, Query(ge=1, le=MAX_CLIPS)] = 2,
               usage: Annotated[str | None, Query(pattern="^never$")] = None,
               target_duration_ms: Annotated[int | None, Query(ge=1000, le=MAX_TARGET_MS)] = None):
    ranking = vector_repo.plan_clips(conn, embed, q=q, search="hybrid", like=[], min_similarity=None, sort=None,
                                     diversify=True)
    found, _ = clips_repo.list_clips(conn, clips_repo.ClipFilters(q=q, usage=usage, ranking=ranking), base=base,
                                     sort=None, include=set(), limit=limit, cursor=None,
                                     max_per_episode=max_per_episode, diversify=True)
    found = without_overlaps(found)
    space = load(conn, embed, "Thread")
    vectors = [None if (row := space.row(c["id"])) is None else space.x[row] for c in found]
    order = chain(vectors)
    clips = [found[i] for i in order]
    hops = [similarity(vectors[a], vectors[b]) for a, b in pairwise(order)]
    resolved = comp_repo.resolve_items(conn, [{"clip_id": c["id"]} for c in clips], "segments")
    return {"q": q, "clips": clips, "hops": hops, "duration_ms": sum(c["duration_ms"] for c in clips),
            "review": review_repo.review_segments(conn, resolved, embed.model_name, target_duration_ms)}


def without_overlaps(clips: list[dict]) -> list[dict]:
    """The clips, best first, less any that shares words with a better one (two runs of one video can)."""
    kept: list[dict] = []
    for clip in clips:
        first, last = clip["words"]["first"], clip["words"]["last"]
        if not any(k["transcript_id"] == clip["transcript_id"] and k["words"]["first"] <= last
                   and first <= k["words"]["last"] for k in kept):
            kept.append(clip)
    return kept


def chain(vectors: list[np.ndarray | None]) -> list[int]:
    """An order of the items where each leads into the next: the first stays first, then each next one is
    the remaining item most like the one before it. Items with no vector keep their order, at the end."""
    if not vectors:
        return []
    order = [0]
    remaining = [i for i in range(1, len(vectors)) if vectors[i] is not None]
    last = 0 if vectors[0] is not None else None
    while remaining:
        nxt = remaining[0] if last is None else max(remaining, key=lambda i: (similarity(vectors[last], vectors[i]), -i))
        order.append(nxt)
        remaining.remove(nxt)
        last = nxt
    return order + [i for i in range(1, len(vectors)) if vectors[i] is None]


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    return None if a is None or b is None else round(float(a @ b), 4)
