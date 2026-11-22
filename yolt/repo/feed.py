"""The feed (the TV page): which clips it may pick from, its picks with their timed words, and its play counts."""

from __future__ import annotations

import random

import psycopg

from ..domain import feed as picker
from . import clips as clips_repo
from .common import iso

FEED_INCLUDES = {"quotes", "speakers"}


def candidates(conn: psycopg.Connection, f: clips_repo.ClipFilters) -> list[picker.Candidate]:
    """Every clip GET /clips would return with the same filters, with its age and feed plays."""
    sql, params = clips_repo.matched_sql(
        f, joins="LEFT JOIN feed_plays fp ON fp.clip_id = c.id",
        columns=", e.id AS _episode, extract(epoch FROM now() - c.created_at)::double precision AS _age, "
                "COALESCE(fp.play_count, 0) AS _plays")
    seen: set[int] = set()
    out = []
    for r in conn.execute(f"{sql} ORDER BY c.id", clips_repo.query_params(f, params)):
        if r["_id"] in seen:
            continue
        seen.add(r["_id"])
        out.append(picker.Candidate(r["_id"], r["_episode"], max(0.0, float(r["_age"])), r["_plays"]))
    return out


def episode_of(conn: psycopg.Connection, clip_id: int) -> int | None:
    row = conn.execute("SELECT r.episode_id FROM clips c JOIN runs r ON r.id = c.run_id WHERE c.id = %s",
                       (clip_id,)).fetchone()
    return row["episode_id"] if row else None


def batch(conn: psycopg.Connection, f: clips_repo.ClipFilters, *, recent: list[int], count: int,
          weights: tuple[int, ...], base: str, rng: random.Random | None = None) -> dict:
    pool = candidates(conn, f)
    previous = episode_of(conn, recent[-1]) if recent else None
    picks = picker.select(pool, recent=recent, previous_episode=previous, count=count, weights=weights,
                          rng=rng or random.Random())
    ids = [candidate.clip_id for candidate, _ in picks]
    clips = {clip["id"]: clip for clip in clips_repo.load_clips(conn, ids, FEED_INCLUDES, base)}
    words = clips_repo.clip_words(conn, ids)
    data = [{
        "clip": clips[candidate.clip_id],
        "tier": tier,
        "plays": candidate.plays,
        "words": [{"seq": w.seq, "word": w.word, "start_ms": w.start_ms, "end_ms": w.end_ms}
                  for w in words[candidate.clip_id].in_order()],
    } for candidate, tier in picks if candidate.clip_id in clips and candidate.clip_id in words]
    return {"data": data, "candidates": len(pool),
            "mix": dict(zip(picker.TIER_NAMES, weights, strict=True))}


def record_play(conn: psycopg.Connection, clip_id: int) -> dict | None:
    """Count a play of the clip; None when there's no such clip."""
    row = conn.execute(
        """INSERT INTO feed_plays (clip_id, play_count) SELECT id, 1 FROM clips WHERE id = %s
           ON CONFLICT (clip_id) DO UPDATE SET play_count = feed_plays.play_count + 1, last_played_at = now()
           RETURNING clip_id, play_count, last_played_at""", (clip_id,)).fetchone()
    if row is None:
        return None
    return {"clip_id": row["clip_id"], "plays": row["play_count"], "last_played_at": iso(row["last_played_at"])}
